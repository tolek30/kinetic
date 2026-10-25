from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import sys
import time
import uuid
import warnings
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image
import uvicorn

ROOT = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))
PUBLIC_DIR = RESOURCE_DIR / "public"
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KINETIC"
else:
    DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
CONFIG_FILE = DATA_DIR / "config.json"
ROOMS_DIR = DATA_DIR / "rooms"
HOST_SETTINGS_FILE = ROOMS_DIR / "host-settings.json"
PORT = int(os.environ.get("PORT", "7399"))
HOST = os.environ.get("HOST", "0.0.0.0")
ROLE = "host"
DESKTOP_MODE = False
APP_VERSION = "2.0.0"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 100_000_000
STAGE_WIDTH = 1920
STAGE_HEIGHT = 1080
STARTED_AT = time.monotonic()
ROOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KINETIC"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ROOMS_DIR.mkdir(parents=True, exist_ok=True)
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def layer_defaults(kind: str) -> dict[str, Any]:
    x, y = (STAGE_WIDTH / 2, STAGE_HEIGHT) if kind == "body" else (760, 700)
    return {"src": None, "x": x, "y": y, "scale": 1, "rotation": 0, "opacity": 1,
            "visible": True, "naturalWidth": None, "naturalHeight": None}


def default_config() -> dict[str, Any]:
    return {"version": 3, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage": {"width": STAGE_WIDTH, "height": STAGE_HEIGHT},
            "assets": {"body": layer_defaults("body"), "head": layer_defaults("head")},
            "talk": {"enabled": True, "swing": 18, "jitter": 0.72, "sensitivity": 3.2,
                     "threshold": 0.05, "smoothing": 0.1},
            "overlay": {"background": "transparent"}}


def clamp_number(value: Any, fallback: float, low: float, high: float) -> float:
    if isinstance(value, bool):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(high, max(low, number)) if math.isfinite(number) else fallback


def safe_bool(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def sanitize_layer(layer: Any, kind: str, room_id: str = "host") -> dict[str, Any]:
    fallback = layer_defaults(kind)
    source = safe_dict(layer)
    src = source.get("src")
    if not isinstance(src, str):
        src = ""
    valid_src = (
        re.fullmatch(r"/uploads/[A-Za-z0-9._-]+", src or "")
        if room_id == "host"
        else re.fullmatch(rf"/room-uploads/{re.escape(room_id)}/[A-Za-z0-9._-]+", src or "")
    )
    if not valid_src:
        src = None
    width, height = source.get("naturalWidth"), source.get("naturalHeight")
    return {"src": src, "x": clamp_number(source.get("x"), fallback["x"], -4000, 6000),
            "y": clamp_number(source.get("y"), fallback["y"], -4000, 6000),
            "scale": clamp_number(source.get("scale"), 1, 0.01, 30),
            "rotation": clamp_number(source.get("rotation"), 0, -360, 360),
            "opacity": clamp_number(source.get("opacity"), 1, 0, 1),
            "visible": safe_bool(source.get("visible"), True),
            "naturalWidth": round(clamp_number(width, 0, 1, MAX_IMAGE_DIMENSION)) if width else None,
            "naturalHeight": round(clamp_number(height, 0, 1, MAX_IMAGE_DIMENSION)) if height else None}


def sanitize_config(value: Any, room_id: str = "host") -> dict[str, Any]:
    base, source = default_config(), safe_dict(value)
    assets, talk, overlay = safe_dict(source.get("assets")), safe_dict(source.get("talk")), safe_dict(source.get("overlay"))
    head = sanitize_layer(assets.get("head"), "head", room_id)
    version = clamp_number(source.get("version"), 0, 0, 100)
    if head["src"] and version < 2:
        if head["naturalWidth"] and head["naturalHeight"]:
            head["x"] -= head["naturalWidth"] * head["scale"] / 2
            head["y"] += head["naturalHeight"] * head["scale"] / 2
        else:
            head["x"], head["y"] = 760, 700
    old_amount = talk.get("amount")
    swing = clamp_number(old_amount, 54, 0, 180) / 3 if version < 3 and old_amount is not None else clamp_number(talk.get("swing"), 18, 0, 35)
    return {"version": 3, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage": {"width": STAGE_WIDTH, "height": STAGE_HEIGHT},
            "assets": {"body": sanitize_layer(assets.get("body"), "body", room_id), "head": head},
            "talk": {"enabled": safe_bool(talk.get("enabled"), True), "swing": swing,
                     "jitter": clamp_number(talk.get("jitter"), 0.72, 0, 1),
                     "sensitivity": clamp_number(talk.get("sensitivity"), 3.2, 0.2, 8),
                     "threshold": clamp_number(talk.get("threshold"), 0.05, 0, 0.4),
                     "smoothing": clamp_number(talk.get("smoothing"), 0.1, 0, 0.9)},
            "overlay": {"background": overlay.get("background") if overlay.get("background") in {"transparent", "black", "white", "green"} else "transparent"}}


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def room_paths(room_id: str) -> tuple[Path, Path]:
    if room_id == "host":
        return CONFIG_FILE, UPLOAD_DIR
    if not ROOM_ID_RE.fullmatch(room_id):
        raise ValueError("Invalid room id")
    folder = ROOMS_DIR / room_id
    return folder / "config.json", folder / "uploads"


def sanitize_mic_devices(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value[:32]:
        row = safe_dict(item)
        device_id = row.get("deviceId")
        if not isinstance(device_id, str) or not device_id or len(device_id) > 512 or device_id in seen:
            continue
        label = row.get("label") if isinstance(row.get("label"), str) else "Microphone"
        devices.append({"deviceId": device_id, "label": label[:120] or "Microphone"})
        seen.add(device_id)
    return devices


def save_room_metadata(room: dict[str, Any]) -> None:
    if room["id"] == "host":
        host_settings["hostRoomMirrorOutput"] = bool(room["mirrorOutput"])
        atomic_write(HOST_SETTINGS_FILE, host_settings)
        return
    atomic_write(ROOMS_DIR / room["id"] / "room.json", {
        "id": room["id"],
        "name": room["name"],
        "token": room["token"],
        "clientKey": room["clientKey"],
        "micDevices": room["micDevices"],
        "selectedMic": room["selectedMic"],
        "mirrorOutput": bool(room["mirrorOutput"]),
    })


def load_room_config(room_id: str) -> dict[str, Any]:
    config_path, _ = room_paths(room_id)
    raw = read_json(config_path, default_config())
    normalized = sanitize_config(raw, room_id)
    if safe_dict(raw).get("version") != normalized["version"]:
        with suppress(OSError):
            atomic_write(config_path, normalized)
    return normalized


host_settings = read_json(HOST_SETTINGS_FILE, {"name": "Host", "accessCode": "", "maxRooms": 10, "publicUrl": "", "mirrorOutput": False})
LEGACY_MIRROR_DEFAULT = safe_bool(host_settings.get("mirrorOutput"), False)
host_settings = {"name": str(host_settings.get("name", "Host"))[:80], "accessCode": str(host_settings.get("accessCode", ""))[:128],
                 "maxRooms": int(clamp_number(host_settings.get("maxRooms"), 10, 1, 10)),
                 "publicUrl": str(host_settings.get("publicUrl", ""))[:512],
                 "mirrorOutput": safe_bool(host_settings.get("mirrorOutput"), False),
                 "hostRoomMirrorOutput": safe_bool(host_settings.get("hostRoomMirrorOutput"), LEGACY_MIRROR_DEFAULT)}
rooms: dict[str, dict[str, Any]] = {"host": {"id": "host", "name": host_settings["name"], "token": "", "clientKey": "host", "config": load_room_config("host"), "revision": 1, "clients": {}, "state": {"talk": 0, "level": 0, "audio": False}, "micDevices": [], "selectedMic": None, "mirrorOutput": safe_bool(host_settings.get("hostRoomMirrorOutput"), LEGACY_MIRROR_DEFAULT)}}
for room_metadata_path in ROOMS_DIR.glob("*/room.json"):
    room_metadata = read_json(room_metadata_path, {})
    room_id = room_metadata.get("id", "") if isinstance(room_metadata, dict) else ""
    if not isinstance(room_id, str) or not ROOM_ID_RE.fullmatch(room_id) or room_id == "host":
        continue
    rooms[room_id] = {"id": room_id, "name": str(room_metadata.get("name", "Instance"))[:80],
                      "token": str(room_metadata.get("token", "")), "clientKey": str(room_metadata.get("clientKey", "")),
                      "config": load_room_config(room_id), "revision": 1, "clients": {},
                      "state": {"talk": 0, "level": 0, "audio": False},
                      "micDevices": sanitize_mic_devices(room_metadata.get("micDevices")),
                      "selectedMic": str(room_metadata["selectedMic"])[:512] if isinstance(room_metadata.get("selectedMic"), str) else None,
                      "mirrorOutput": safe_bool(room_metadata.get("mirrorOutput"), LEGACY_MIRROR_DEFAULT)}
config = rooms["host"]["config"]
revision = 1
clients: dict[WebSocket, dict[str, Any]] = rooms["host"]["clients"]
save_tasks: dict[str, asyncio.Task[None]] = {}
MODE_SETTINGS: dict[str, Any] = {}
UPSTREAM = ""
INSTANCE_ID = ""
INSTANCE_TOKEN = ""
INSTANCE_NAME = ""
LOCAL_PORT = PORT


def configure_host(settings: dict, port: int) -> None:
    global ROLE, PORT, HOST, MODE_SETTINGS, host_settings, DESKTOP_MODE
    ROLE, HOST, PORT = "host", "0.0.0.0", int(port)
    MODE_SETTINGS = dict(settings or {})
    DESKTOP_MODE = bool(MODE_SETTINGS.get("desktop", False))
    if any(key in MODE_SETTINGS for key in ("name", "accessCode", "maxRooms", "publicUrl")):
        host_settings = {
            "name": str(MODE_SETTINGS.get("name", host_settings["name"])).strip()[:80] or "Host",
            "accessCode": str(MODE_SETTINGS.get("accessCode", host_settings["accessCode"]))[:128],
            "maxRooms": int(clamp_number(MODE_SETTINGS.get("maxRooms"), host_settings["maxRooms"], 1, 10)),
            "publicUrl": normalize_public_url(MODE_SETTINGS.get("publicUrl", host_settings["publicUrl"]), int(port)),
            "mirrorOutput": host_settings["mirrorOutput"],
            "hostRoomMirrorOutput": host_settings.get("hostRoomMirrorOutput", LEGACY_MIRROR_DEFAULT),
        }
        rooms["host"]["name"] = host_settings["name"]
        atomic_write(HOST_SETTINGS_FILE, host_settings)


def configure_instance(settings: dict, local_port: int) -> None:
    global ROLE, HOST, PORT, LOCAL_PORT, MODE_SETTINGS, UPSTREAM, INSTANCE_ID, INSTANCE_TOKEN, INSTANCE_NAME, DESKTOP_MODE
    ROLE, HOST, PORT = "instance", "127.0.0.1", int(local_port)
    DESKTOP_MODE = True
    LOCAL_PORT, MODE_SETTINGS = int(local_port), dict(settings or {})
    UPSTREAM = str(MODE_SETTINGS.get("hostUrl", MODE_SETTINGS.get("host_url", MODE_SETTINGS.get("url", "")))).rstrip("/")
    INSTANCE_ID = str(MODE_SETTINGS.get("instanceId", MODE_SETTINGS.get("instance_id", MODE_SETTINGS.get("id", ""))))
    INSTANCE_TOKEN = str(MODE_SETTINGS.get("token", ""))
    INSTANCE_NAME = str(MODE_SETTINGS.get("roomName", MODE_SETTINGS.get("room_name", MODE_SETTINGS.get("name", "Instance"))))


def lan_addresses() -> list[str]:
    addresses: list[str] = []
    with suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            candidate = probe.getsockname()[0]
            if not candidate.startswith("127."):
                addresses.append(candidate)
    with suppress(OSError):
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidate = result[4][0]
            if not candidate.startswith(("127.", "169.254.")) and candidate not in addresses:
                addresses.append(candidate)
    return addresses


def normalize_public_url(value: Any, default_port: int = PORT) -> str:
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public IP or domain with http:// or https://.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Public URL must be a base address without a path or query.")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Public URL contains an invalid port.") from error
    hostname = parsed.hostname
    hostpart = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        hostpart += f":{port}"
    elif parsed.scheme == "http":
        hostpart += f":{default_port}"
    return urlunsplit((parsed.scheme, hostpart, "", "", ""))


try:
    host_settings["publicUrl"] = normalize_public_url(host_settings["publicUrl"], PORT)
except ValueError:
    host_settings["publicUrl"] = ""


def host_base_urls() -> list[str]:
    return [f"http://{ip}:{PORT}" for ip in lan_addresses()] if ROLE == "host" and HOST in {"0.0.0.0", "::"} else []


def overlay_url(room_id: str, base: str | None = None) -> str:
    return f"{(base or f'http://localhost:{PORT}')}/overlay/{quote(room_id, safe='')}"


def client_counts(room: dict[str, Any]) -> dict[str, int]:
    rows = list(room["clients"].values())
    controllers = sum(c["role"] == "controller" and not c.get("hostAdmin") for c in rows)
    overlays = sum(c["role"] == "overlay" for c in rows)
    return {"controllers": controllers, "overlays": overlays, "total": controllers + overlays}


async def ws_send(ws: WebSocket, payload: dict[str, Any], room: dict[str, Any]) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        room["clients"].pop(ws, None)
        return False


async def room_broadcast(room: dict[str, Any], payload: dict[str, Any], predicate=lambda _c: True) -> None:
    for ws, client in list(room["clients"].items()):
        if predicate(client):
            await ws_send(ws, payload, room)


async def announce(room: dict[str, Any]) -> None:
    counts = client_counts(room)
    await room_broadcast(room, {"type": "clients", **counts})


async def save_room(room_id: str) -> None:
    await asyncio.sleep(0.25)
    config_path, _ = room_paths(room_id)
    with suppress(OSError):
        await asyncio.to_thread(atomic_write, config_path, rooms[room_id]["config"])


def schedule_save(room_id: str) -> None:
    task = save_tasks.get(room_id)
    if task and not task.done():
        task.cancel()
    save_tasks[room_id] = asyncio.create_task(save_room(room_id))


async def update_room_config(room_id: str, value: Any, source_client_id: str | None = None) -> dict[str, Any]:
    room = rooms[room_id]
    room["config"] = sanitize_config(value, room_id)
    room["revision"] += 1
    if room_id == "host":
        global config, revision
        config, revision = room["config"], room["revision"]
    schedule_save(room_id)
    await room_broadcast(room, {"type": "config", "config": room["config"], "revision": room["revision"]}, lambda c: c.get("clientId") != source_client_id)
    return room["config"]


app = FastAPI(title="KINETIC LAN Server", docs_url=None, redoc_url=None)


@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/") or request.url.path in {"/", "/overlay", "/host"}:
        response.headers["Cache-Control"] = "no-store"
    return response


def room_for_request(request: Request, allow_public_read: bool = False) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    room_id = request.query_params.get("instanceId", "host")
    if room_id == "" or not ROOM_ID_RE.fullmatch(room_id):
        return None, JSONResponse({"error": "Invalid room."}, status_code=400)
    room = rooms.get(room_id)
    if room is None:
        return None, JSONResponse({"error": "Room not found."}, status_code=404)
    peer = request.client.host if request.client else ""
    if room_id == "host" and peer not in {"127.0.0.1", "::1", "localhost"} and not allow_public_read:
        return None, JSONResponse({"error": "Host room data is local-only."}, status_code=403)
    if room_id != "host" and request.headers.get("x-instance-token") != room["token"] and not allow_public_read and not host_admin_is_local(request):
        return None, JSONResponse({"error": "Invalid room token."}, status_code=401)
    return room, None


def host_admin_is_local(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    return peer in {"127.0.0.1", "::1", "localhost"}


async def proxy_request(method: str, path: str, body: bytes = b"") -> Response:
    import urllib.error
    import urllib.request
    url = UPSTREAM + path
    def request_sync():
        request = urllib.request.Request(url, data=body if method not in {"GET", "HEAD"} else None,
                                         method=method, headers={"X-Instance-Token": INSTANCE_TOKEN,
                                         "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as upstream:
                return upstream.status, upstream.headers.get("content-type", "application/json"), upstream.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("content-type", "application/json"), error.read()
    try:
        status, content_type, payload = await asyncio.to_thread(request_sync)
        return Response(payload, status_code=status, media_type=content_type.split(";")[0])
    except Exception:
        return JSONResponse({"error": "Host is unavailable."}, status_code=502)


@app.get("/api/release")
async def check_release():
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/tolek30/kinetic/releases/latest",
            headers={"User-Agent": "KINETIC-App"}
        )
        def fetch_release():
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        data = await asyncio.to_thread(fetch_release)
        tag_name = data.get("tag_name", "")
        name = data.get("name", "")
        version_source = name if ("." in name) else (tag_name if "." in tag_name else (name or tag_name))
        latest_version = version_source.lstrip("v").strip()
        release_title = name or tag_name or "New release"
        body = data.get("body", "")
        download_url = None
        for asset in data.get("assets", []):
            asset_name = asset.get("name", "")
            if asset_name.lower().startswith("kinetic") and asset_name.lower().endswith(".exe"):
                download_url = asset.get("browser_download_url")
                break
        if not download_url:
            download_url = data.get("html_url")
        current = APP_VERSION
        update_available = bool(latest_version and latest_version != current)
        return {
            "ok": True,
            "currentVersion": current,
            "latestVersion": latest_version or current,
            "updateAvailable": update_available,
            "releaseName": release_title,
            "releaseNotes": body[:500],
            "downloadUrl": download_url
        }
    except Exception as e:
        return {"ok": False, "currentVersion": APP_VERSION, "latestVersion": APP_VERSION, "updateAvailable": False, "error": str(e)}


@app.get("/api/health")
async def health(request: Request):
    if ROLE == "instance":
        base = UPSTREAM
        upstream_health = await proxy_request("GET", "/api/health?instanceId=" + quote(INSTANCE_ID, safe=""))
        try:
            upstream_data = json.loads(upstream_health.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            upstream_data = {}
        share_url = (
            overlay_url(INSTANCE_ID, base) if base and INSTANCE_ID else None
        ) or upstream_data.get("publicOverlayUrl") or (
            upstream_data.get("networkUrls", [None])[0] if upstream_data.get("networkUrls") else None
        )
        return {"ok": True, "role": ROLE, "instanceId": INSTANCE_ID, "version": 7,
                "appVersion": APP_VERSION,
                "desktop": DESKTOP_MODE,
                "port": PORT, "revision": 0, "uptime": int(time.monotonic() - STARTED_AT), "clients": {},
                "networkUrls": [share_url] if share_url else [],
                "overlayUrl": overlay_url(INSTANCE_ID, base) if base and INSTANCE_ID else None,
                "publicOverlayUrl": share_url,
                "publicUrl": upstream_data.get("publicUrl", ""),
                "mirrorOutput": bool(upstream_data.get("mirrorOutput", False))}
    room_id = request.query_params.get("instanceId", "host")
    room = rooms.get(room_id) if ROOM_ID_RE.fullmatch(room_id) else None
    is_local = host_admin_is_local(request)
    network_urls = [overlay_url(room_id, base) for base in host_base_urls()] if room and is_local else []
    public_url = host_settings.get("publicUrl", "")
    public_overlay = overlay_url(room_id, public_url) if public_url and room else None
    return {"ok": True, "role": ROLE, "capacity": host_settings["maxRooms"], "port": PORT, "version": 7,
            "appVersion": APP_VERSION,
            "revision": room["revision"] if room else 0, "uptime": int(time.monotonic() - STARTED_AT),
            "clients": client_counts(room) if room else {}, "desktop": DESKTOP_MODE, "networkUrls": network_urls,
            "overlayUrl": overlay_url(room_id) if room and is_local else public_overlay,
            "publicUrl": public_url, "publicOverlayUrl": public_overlay,
            "mirrorOutput": bool(room.get("mirrorOutput", False)) if room else False}


@app.post("/api/instances/join")
async def join_instance(request: Request):
    global host_settings
    if ROLE == "instance":
        return JSONResponse({"error": "Not available in instance mode."}, status_code=404)
    raw = await request.body()
    if len(raw) > 16384:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    body = safe_dict(body)
    if str(body.get("accessCode", "")) != host_settings["accessCode"]:
        return JSONResponse({"error": "Invalid access code."}, status_code=403)
    key = str(body.get("clientKey", ""))
    name = str(body.get("name", "Instance")).strip()[:80] or "Instance"
    if not key or len(key) > 200:
        return JSONResponse({"error": "A valid clientKey is required."}, status_code=400)
    room = next((r for r in rooms.values() if r["id"] != "host" and secrets.compare_digest(r["clientKey"], key)), None)
    if room is None:
        if len(rooms) >= host_settings["maxRooms"]:
            return JSONResponse({"error": "Room capacity reached."}, status_code=409)
        room_id = uuid.uuid4().hex[:16]
        folder = ROOMS_DIR / room_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "uploads").mkdir(exist_ok=True)
        room = {"id": room_id, "name": name, "token": secrets.token_urlsafe(32), "clientKey": key,
                "config": default_config(), "revision": 1, "clients": {}, "state": {"talk": 0, "level": 0, "audio": False},
                "micDevices": [], "selectedMic": "", "mirrorOutput": False}
        rooms[room_id] = room
        atomic_write(folder / "config.json", room["config"])
        save_room_metadata(room)
    else:
        room["name"] = name
        save_room_metadata(room)
    return {"instance": {"id": room["id"], "token": room["token"], "name": room["name"]},
            "config": room["config"], "overlayUrl": overlay_url(room["id"])}


@app.get("/api/host")
async def get_host(request: Request):
    if ROLE == "instance":
        return JSONResponse({"error": "Not available in instance mode."}, status_code=404)
    if not host_admin_is_local(request):
        return JSONResponse({"error": "Host settings are local-only."}, status_code=403)
    return {"settings": {**host_settings, "accessCode": host_settings["accessCode"]},
            "rooms": [{"id": r["id"], "name": r["name"], "overlayUrl": overlay_url(r["id"]),
                       "networkUrls": [overlay_url(r["id"], base) for base in host_base_urls()],
                        "clients": client_counts(r), "liveState": r["state"],
                        "micDevices": r["micDevices"], "selectedMic": r["selectedMic"],
                        "mirrorOutput": bool(r.get("mirrorOutput", False))} for r in rooms.values()]}


@app.get("/api/public-address")
async def detect_public_address(request: Request):
    if ROLE == "instance" or not host_admin_is_local(request):
        return JSONResponse({"error": "Public address detection is Host-local."}, status_code=403)
    try:
        def fetch_address():
            import urllib.request
            with urllib.request.urlopen("https://api.ipify.org", timeout=5) as response:
                return response.read(128).decode("ascii").strip()
        address = await asyncio.to_thread(fetch_address)
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("Detected address is not public")
        return {"ok": True, "publicUrl": f"http://{address}:{PORT}"}
    except Exception:
        return JSONResponse({"error": "Could not detect a public address. Enter your public IP or domain manually."}, status_code=502)


@app.put("/api/host/settings")
async def put_host_settings(request: Request):
    global host_settings
    if ROLE == "instance":
        return JSONResponse({"error": "Not available in instance mode."}, status_code=404)
    if not host_admin_is_local(request):
        return JSONResponse({"error": "Host settings are local-only."}, status_code=403)
    try:
        body = safe_dict(json.loads(await request.body()))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    try:
        public_url = normalize_public_url(body.get("publicUrl", host_settings["publicUrl"]), PORT)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    host_settings = {"name": str(body.get("name", host_settings["name"])).strip()[:80] or "Host",
                     "accessCode": str(body.get("accessCode", host_settings["accessCode"]))[:128],
                     "maxRooms": int(clamp_number(body.get("maxRooms"), host_settings["maxRooms"], 1, 10)),
                     "publicUrl": public_url,
                     "mirrorOutput": host_settings.get("mirrorOutput", LEGACY_MIRROR_DEFAULT),
                     "hostRoomMirrorOutput": host_settings.get("hostRoomMirrorOutput", LEGACY_MIRROR_DEFAULT)}
    rooms["host"]["name"] = host_settings["name"]
    atomic_write(HOST_SETTINGS_FILE, host_settings)
    return {"ok": True, "settings": host_settings}


@app.delete("/api/instances/{instance_id}")
async def delete_instance(instance_id: str, request: Request):
    if ROLE == "instance":
        return JSONResponse({"error": "Not available in instance mode."}, status_code=404)
    if not host_admin_is_local(request):
        return JSONResponse({"error": "Instance management is local-only."}, status_code=403)
    if instance_id == "host" or not ROOM_ID_RE.fullmatch(instance_id):
        return JSONResponse({"error": "The Host camera cannot be removed here."}, status_code=400)
    room = rooms.pop(instance_id, None)
    if not room:
        return JSONResponse({"error": "Instance not found."}, status_code=404)
    task = save_tasks.pop(instance_id, None)
    if task and not task.done():
        task.cancel()
    for websocket in list(room["clients"]):
        with suppress(Exception):
            await websocket.close(code=1012, reason="Instance removed by Host")
    await asyncio.to_thread(shutil.rmtree, ROOMS_DIR / instance_id, True)
    return {"ok": True, "instanceId": instance_id}


@app.put("/api/instances/{instance_id}/microphone")
async def select_instance_microphone(instance_id: str, request: Request):
    if ROLE == "instance":
        return JSONResponse({"error": "Not available in instance mode."}, status_code=404)
    if not host_admin_is_local(request):
        return JSONResponse({"error": "Microphone selection is Host-local."}, status_code=403)
    room = rooms.get(instance_id)
    if not room:
        return JSONResponse({"error": "Instance not found."}, status_code=404)
    try:
        payload = safe_dict(json.loads(await request.body()))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    device_id = payload.get("deviceId", "")
    if not isinstance(device_id, str) or (device_id and device_id not in {item["deviceId"] for item in room["micDevices"]}):
        return JSONResponse({"error": "Choose a microphone reported by this Instance."}, status_code=400)
    room["selectedMic"] = device_id
    save_room_metadata(room)
    await room_broadcast(
        room,
        {"type": "mic-select", "deviceId": device_id},
        lambda client: client["role"] == "controller" and not client.get("hostAdmin"),
    )
    return {"ok": True, "deviceId": device_id}


@app.put("/api/instances/{instance_id}/mirror")
async def set_instance_mirror(instance_id: str, request: Request):
    if ROLE == "instance":
        return JSONResponse({"error": "Not available in instance mode."}, status_code=404)
    if not host_admin_is_local(request):
        return JSONResponse({"error": "Mirror settings are Host-local."}, status_code=403)
    room = rooms.get(instance_id)
    if not room:
        return JSONResponse({"error": "Instance not found."}, status_code=404)
    try:
        body = safe_dict(json.loads(await request.body()))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    room["mirrorOutput"] = safe_bool(body.get("mirrorOutput"), room.get("mirrorOutput", False))
    save_room_metadata(room)
    await room_broadcast(
        room,
        {"type": "room-settings", "mirrorOutput": room["mirrorOutput"]},
        lambda client: client["role"] == "overlay",
    )
    return {"ok": True, "mirrorOutput": room["mirrorOutput"]}


@app.get("/api/config")
async def get_config(request: Request):
    if ROLE == "instance":
        path = "/api/config?instanceId=" + quote(INSTANCE_ID, safe="")
        return await proxy_request("GET", path)
    room, error = room_for_request(request, allow_public_read=True)
    return error or {"config": room["config"], "revision": room["revision"]}


@app.put("/api/config")
async def put_config(request: Request):
    if ROLE == "instance":
        return await proxy_request("PUT", "/api/config?instanceId=" + quote(INSTANCE_ID, safe=""), await request.body())
    room, error = room_for_request(request)
    if error:
        return error
    body = await request.body()
    if len(body) > 1024 * 1024:
        return JSONResponse({"error": "Configuration payload is too large."}, status_code=413)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    saved = await update_room_config(room["id"], safe_dict(payload).get("config"), request.headers.get("x-client-id"))
    return {"ok": True, "config": saved, "revision": room["revision"]}


@app.post("/api/config/reset")
async def reset_config(request: Request):
    if ROLE == "instance":
        return await proxy_request("POST", "/api/config/reset?instanceId=" + quote(INSTANCE_ID, safe=""))
    room, error = room_for_request(request)
    if error:
        return error
    saved = await update_room_config(room["id"], default_config(), request.headers.get("x-client-id"))
    return {"ok": True, "config": saved, "revision": room["revision"]}


@app.post("/api/upload")
async def upload_asset(request: Request, file: UploadFile = File(...)):
    slot = request.query_params.get("slot")
    if slot not in {"body", "head"}:
        return JSONResponse({"error": "Choose a valid asset slot."}, status_code=400)
    if ROLE == "instance":
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "Image is larger than 30 MB."}, status_code=413)
        boundary = "----kinetic" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"asset\"\r\nContent-Type: {file.content_type or 'application/octet-stream'}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        await file.close()
        return await instance_upload_proxy(slot, body, boundary)
    room, error = room_for_request(request)
    if error:
        return error
    extensions = {"image/png": (".png", "PNG"), "image/webp": (".webp", "WEBP"), "image/gif": (".gif", "GIF"), "image/jpeg": (".jpg", "JPEG")}
    mime = (file.content_type or "").lower()
    if mime not in extensions:
        return JSONResponse({"error": "Use a PNG, WebP, GIF or JPEG image."}, status_code=415)
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "Image is larger than 30 MB."}, status_code=413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(content))
            width, height = image.size
            if image.format != extensions[mime][1] or not width or not height or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Unsupported image")
            image.verify()
    except Exception:
        return JSONResponse({"error": "That file is not a supported, readable image."}, status_code=415)
    finally:
        await file.close()
    _, upload_dir = room_paths(room["id"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{time.time_ns()}-{uuid.uuid4().hex[:10]}{extensions[mime][0]}"
    destination = upload_dir / filename
    await asyncio.to_thread(destination.write_bytes, content)
    old_src = room["config"]["assets"][slot].get("src")
    next_config = json.loads(json.dumps(room["config"]))
    url = f"/uploads/{filename}" if room["id"] == "host" else f"/room-uploads/{room['id']}/{filename}"
    next_config["assets"][slot].update({"src": url, "naturalWidth": width, "naturalHeight": height})
    saved = await update_room_config(room["id"], next_config, request.headers.get("x-client-id"))
    if old_src:
        old_match = re.fullmatch(r"/uploads/([A-Za-z0-9._-]+)", old_src) if room["id"] == "host" else re.fullmatch(rf"/room-uploads/{re.escape(room['id'])}/([A-Za-z0-9._-]+)", old_src)
        if old_match:
            with suppress(OSError):
                await asyncio.to_thread((upload_dir / old_match.group(1)).unlink, missing_ok=True)
    return {"ok": True, "slot": slot, "url": url, "width": width, "height": height, "config": saved, "revision": room["revision"]}


async def instance_upload_proxy(slot: str, multipart_body: bytes, boundary: str):
    import urllib.error
    import urllib.request
    url = UPSTREAM + f"/api/upload?slot={slot}&instanceId={quote(INSTANCE_ID, safe='')}"
    def send():
        req = urllib.request.Request(url, data=multipart_body, method="POST", headers={"X-Instance-Token": INSTANCE_TOKEN, "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, res.headers.get("content-type", "application/json"), res.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("content-type", "application/json"), e.read()
    try:
        status, content_type, data = await asyncio.to_thread(send)
        return Response(data, status_code=status, media_type=content_type.split(";")[0])
    except Exception:
        return JSONResponse({"error": "Host is unavailable."}, status_code=502)


@app.get("/overlay")
async def overlay_alias():
    return FileResponse(PUBLIC_DIR / "overlay.html", headers={"Cache-Control": "no-store"})


@app.get("/overlay/{instance_id}")
async def overlay_page(instance_id: str):
    if not ROOM_ID_RE.fullmatch(instance_id) or instance_id not in rooms:
        return FileResponse(PUBLIC_DIR / "404.html", status_code=404)
    return FileResponse(PUBLIC_DIR / "overlay.html", headers={"Cache-Control": "no-store"})


async def send_file(path: Path):
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"}) if path.is_file() else FileResponse(PUBLIC_DIR / "404.html", status_code=404)


@app.get("/uploads/{filename}")
async def uploaded_asset(filename: str):
    if ROLE == "instance":
        return FileResponse(PUBLIC_DIR / "404.html", status_code=404)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        return FileResponse(PUBLIC_DIR / "404.html", status_code=404)
    path = (UPLOAD_DIR / filename).resolve()
    with suppress(ValueError):
        path.relative_to(UPLOAD_DIR.resolve())
        return await send_file(path)
    return FileResponse(PUBLIC_DIR / "404.html", status_code=404)


@app.get("/room-uploads/{instance_id}/{filename}")
async def room_uploaded_asset(instance_id: str, filename: str):
    if ROLE == "instance":
        return await proxy_request("GET", f"/room-uploads/{quote(instance_id, safe='')}/{quote(filename, safe='')}")
    if not ROOM_ID_RE.fullmatch(instance_id) or not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        return FileResponse(PUBLIC_DIR / "404.html", status_code=404)
    try:
        _, upload_dir = room_paths(instance_id)
    except ValueError:
        return FileResponse(PUBLIC_DIR / "404.html", status_code=404)
    path = (upload_dir / filename).resolve()
    with suppress(ValueError):
        path.relative_to(upload_dir.resolve())
        return await send_file(path)
    return FileResponse(PUBLIC_DIR / "404.html", status_code=404)


@app.get("/")
async def controller_page():
    return FileResponse(PUBLIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/host")
async def host_page():
    path = PUBLIC_DIR / "host.html"
    return await send_file(path) if path.is_file() else FileResponse(PUBLIC_DIR / "404.html", status_code=404)


async def instance_websocket_proxy(local: WebSocket):
    try:
        import websockets
        import inspect
        await local.accept()
        upstream_url = UPSTREAM.replace("https://", "wss://").replace("http://", "ws://") + "/ws?instanceId=" + quote(INSTANCE_ID, safe="")
        header_option = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
        upstream = await websockets.connect(upstream_url, max_size=1024 * 1024, **{header_option: {"X-Instance-Token": INSTANCE_TOKEN}})
        async def to_host():
            while True:
                data = await local.receive_text()
                try:
                    message = json.loads(data)
                    if isinstance(message, dict) and message.get("type") == "hello":
                        message["instanceId"] = INSTANCE_ID
                        message["token"] = INSTANCE_TOKEN
                        data = json.dumps(message)
                except json.JSONDecodeError:
                    pass
                await upstream.send(data)
        async def to_local():
            async for data in upstream:
                await local.send_text(data)
        tasks = [asyncio.create_task(to_host()), asyncio.create_task(to_local())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
    except WebSocketDisconnect:
        pass
    except Exception:
        with suppress(Exception):
            await local.close(code=1011, reason="Host unavailable")


@app.websocket("/ws")
async def websocket_relay(websocket: WebSocket):
    if ROLE == "instance":
        await instance_websocket_proxy(websocket)
        return
    await websocket.accept()
    try:
        first = await websocket.receive_text()
        message = json.loads(first)
    except Exception:
        await websocket.close(code=1008)
        return
    if not isinstance(message, dict) or message.get("type") != "hello":
        await websocket.close(code=1008)
        return
    room_id = message.get("instanceId", websocket.query_params.get("instanceId", "host"))
    if not isinstance(room_id, str) or not ROOM_ID_RE.fullmatch(room_id) or room_id not in rooms:
        await websocket.close(code=1008, reason="Unknown room")
        return
    room = rooms[room_id]
    peer = websocket.client.host if websocket.client else ""
    is_loopback = peer in {"127.0.0.1", "::1", "localhost"}
    role = message.get("role")
    token = str(message.get("token", websocket.headers.get("x-instance-token", "")))
    is_host_admin = ROLE == "host" and room_id != "host" and is_loopback and not secrets.compare_digest(token, room["token"])
    if role == "controller" and room_id != "host" and not secrets.compare_digest(token, room["token"]) and not is_host_admin:
        await websocket.close(code=1008, reason="Invalid room token")
        return
    if role == "controller" and room_id == "host" and not is_loopback and token != room["token"]:
        await websocket.close(code=1008, reason="Controller must be local")
        return
    if role not in {"controller", "overlay"}:
        await websocket.close(code=1008)
        return
    client = {"role": role, "clientId": message.get("clientId") if isinstance(message.get("clientId"), str) else None,
              "hostAdmin": is_host_admin}
    room["clients"][websocket] = client
    try:
        await ws_send(websocket, {"type": "snapshot", "config": room["config"], "revision": room["revision"],
                                 "clients": client_counts(room), "micDeviceId": room["selectedMic"],
                                 "hostSettings": {"mirrorOutput": bool(host_settings.get("mirrorOutput", False))},
                                 "roomSettings": {"mirrorOutput": bool(room.get("mirrorOutput", False))}}, room)
        if role == "overlay":
            await ws_send(websocket, {"type": "state", "state": room["state"]}, room)
        await announce(room)
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 1024 * 1024:
                await websocket.close(code=1009, reason="Message too large")
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "state" and role == "controller":
                state = safe_dict(msg.get("state"))
                room["state"] = {"talk": clamp_number(state.get("talk"), 0, 0, 1), "level": clamp_number(state.get("level"), 0, 0, 1), "audio": bool(state.get("audio")), "at": int(time.time() * 1000)}
                await room_broadcast(room, {"type": "state", "state": room["state"]}, lambda c: c["role"] == "overlay")
            elif msg.get("type") == "mic-devices" and role == "controller" and not client["hostAdmin"]:
                room["micDevices"] = sanitize_mic_devices(msg.get("devices"))
                selected = msg.get("selectedDeviceId")
                if isinstance(selected, str):
                    room["selectedMic"] = selected
                if room["selectedMic"] and room["selectedMic"] not in {d["deviceId"] for d in room["micDevices"]}:
                    room["selectedMic"] = ""
                    await room_broadcast(
                        room,
                        {"type": "mic-select", "deviceId": ""},
                        lambda c: c["role"] == "controller" and not c.get("hostAdmin"),
                    )
                save_room_metadata(room)
            elif msg.get("type") == "config" and role == "controller":
                saved = await update_room_config(room_id, msg.get("config"), client.get("clientId"))
                await ws_send(websocket, {"type": "saved", "config": saved, "revision": room["revision"]}, room)
            elif msg.get("type") == "ping":
                await ws_send(websocket, {"type": "pong", "at": int(time.time() * 1000)}, room)
    except WebSocketDisconnect:
        pass
    finally:
        room["clients"].pop(websocket, None)
        await announce(room)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def unknown_api(path: str):
    return JSONResponse({"error": "Unknown API endpoint."}, status_code=404)


@app.get("/{path:path}")
async def static_or_not_found(path: str):
    candidate = (PUBLIC_DIR / path).resolve()
    try:
        candidate.relative_to(PUBLIC_DIR.resolve())
    except ValueError:
        candidate = PUBLIC_DIR / "404.html"
    return FileResponse(candidate) if candidate.is_file() else FileResponse(PUBLIC_DIR / "404.html", status_code=404)


def print_urls() -> None:
    print(f"KINETIC controller: http://localhost:{PORT}/")
    print(f"Host overlay: {overlay_url('host')}")
    for url in host_base_urls():
        print(f"Network overlay: {overlay_url('host', url)}")


if __name__ == "__main__":
    configure_host({}, PORT)
    print_urls()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", ws_max_size=1024 * 1024)
