"""Optional, bounded HLS probe for one locally configured IPTV channel."""
import json
import re
import time
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request

from iptv_monitoring import CHANNEL_ID, TOKEN, direct_opener

SEGMENT = re.compile(r"(?:native\.|[a-f0-9]{24}\.)?([0-9]{1,20})\.ts")


def _download(url, size, deadline, *, segment=False):
    request = Request(url, headers={"User-Agent": "vps-manager-hls-probe/1"})
    if segment:
        request.add_header("Range", f"bytes=0-{size - 1}")
    timeout = min(12, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError
    with direct_opener().open(request, timeout=timeout) as reply:
        if reply.status not in ((200, 206) if segment else (200,)):
            raise ValueError
        body = bytearray()
        while len(body) < size and time.monotonic() < deadline:
            chunk = reply.read1(min(16384, size - len(body)))
            if not chunk:
                return bytes(body)
            body.extend(chunk)
        if segment and len(body) == size:
            return bytes(body)
        raise ValueError


def _playlist(body, prefix):
    lines = body.decode("utf-8").splitlines()
    if not lines or lines[0] != "#EXTM3U" or not any(line.startswith("#EXT-X-TARGETDURATION:") for line in lines):
        raise ValueError
    segments = []
    for line in lines[1:]:
        if not line or line.startswith("#"):
            continue
        if line.startswith(prefix):
            relative = line[len(prefix):]
        elif line.startswith("/"):
            root_path = "/" + prefix.split("/", 3)[3]
            if not line.startswith(root_path):
                raise ValueError
            relative = line[len(root_path):]
        else:
            relative = line
        match = SEGMENT.fullmatch(relative.removeprefix("segment/")) if relative.startswith("segment/") else None
        if not match:
            raise ValueError
        segments.append((relative, int(match[1])))
    if not segments:
        raise ValueError
    return segments[-1]


def probe(config, origin, *, wait_seconds=45, interval=2):
    try:
        path = config / "playback-probe.json"
        if path.is_symlink():
            raise ValueError
        with path.open("rb") as source:
            options = json.loads(source.read(4096))
        token, channel = options["token"], options["channel_id"]
        if not TOKEN.fullmatch(token) or not CHANNEL_ID.fullmatch(channel) or ":" in channel:
            raise ValueError
    except FileNotFoundError:
        return {"ok": None, "state": "not_configured"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ok": None, "state": "invalid_configuration"}

    prefix = f"{origin}/{token}/channel/{channel}/"
    start = time.monotonic()
    deadline = start + wait_seconds
    ready_ms = None
    previous = None
    state = "manifest_unavailable"
    status = None
    while time.monotonic() < deadline:
        try:
            body = _download(prefix + "index.m3u8", 256 * 1024, deadline)
            relative, sequence = _playlist(body, prefix)
        except HTTPError as error:
            with error:
                status = error.code
            state = "manifest_http_error"
            if status != 503:
                break
        except (OSError, HTTPException):
            state, status = "manifest_unavailable", None
        except ValueError:
            state, status = "invalid_manifest", None
            break
        else:
            status = 200
            if ready_ms is None:
                ready_ms = round(1000 * (time.monotonic() - start))
            generation = relative.rsplit("/", 1)[1].rpartition(".")[0].rpartition(".")[0]
            if previous is not None and generation == previous[0] and sequence > previous[1]:
                return _segment(prefix + relative, deadline, channel, ready_ms, start)
            previous = generation, sequence
            state = "playlist_not_advancing"
        time.sleep(max(0, min(interval, deadline - time.monotonic())))
    return {"ok": False, "state": state, "channel_id": channel,
            "manifest_ready_ms": ready_ms, "manifest_http_status": status}


def _segment(url, deadline, channel, ready_ms, start):
    advanced_ms = round(1000 * (time.monotonic() - start))
    result = {"ok": False, "state": "segment_unavailable", "channel_id": channel,
              "manifest_ready_ms": ready_ms, "playlist_advanced_ms": advanced_ms}
    try:
        data = _download(url, 65536, deadline, segment=True)
    except HTTPError as error:
        with error:
            result.update(state="segment_http_error", segment_http_status=error.code)
    except (OSError, HTTPException, ValueError):
        pass
    else:
        result.update(ok=bool(data), state="ok" if data else "empty_segment", segment_bytes=len(data))
    return result
