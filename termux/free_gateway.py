#!/usr/bin/env python3
"""Ponytail Mobile free-API gateway.

A small OpenAI-compatible loopback gateway for OpenCode on Termux. It forwards
requests to healthy free routes, learns from real failures, and keeps upstream
API keys outside the Ubuntu proot.

Stdlib only. Secrets live in a mode-0600 JSON file; environment variables are
accepted only as explicit overrides for tests/automation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets as secrets_module
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("PONYTAIL_MOBILE_HOME", "~/.config/ponytail")).expanduser()
CONFIG_PATH = Path(os.environ.get("PONYTAIL_MOBILE_CONFIG", DEFAULT_HOME / "mobile.json")).expanduser()
STATE_PATH = Path(os.environ.get("PONYTAIL_MOBILE_STATE", DEFAULT_HOME / "mobile-state.json")).expanduser()
DISCOVERY_PATH = Path(os.environ.get("PONYTAIL_MOBILE_DISCOVERY", DEFAULT_HOME / "mobile-discovery.json")).expanduser()
SECRETS_PATH = Path(os.environ.get("PONYTAIL_MOBILE_SECRETS", DEFAULT_HOME / "mobile-secrets.json")).expanduser()
LOCK = threading.RLock()
USER_AGENT = "ponytail-mobile/2"
RATE_HEADERS = (
    "retry-after",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)


def load_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def secure_mode(path: Path) -> bool:
    try:
        return (path.stat().st_mode & 0o077) == 0
    except FileNotFoundError:
        return True


def load_secrets(path: Path | None = None) -> dict:
    path = SECRETS_PATH if path is None else path
    if not path.exists():
        return {}
    if not secure_mode(path):
        raise RuntimeError(f"refusing secrets file with group/world permissions: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise RuntimeError(f"cannot parse secrets file {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("mobile-secrets.json must be a JSON object")
    return data


def save_secret(name: str, value: str) -> None:
    if not re.fullmatch(r"(?:gatewayToken|[A-Z][A-Z0-9_]{2,80})", name):
        raise ValueError("invalid secret name")
    with LOCK:
        data = load_secrets()
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        save_json(SECRETS_PATH, data)


def ensure_secrets() -> dict:
    with LOCK:
        data = load_secrets()
        if not data.get("gatewayToken"):
            data["gatewayToken"] = secrets_module.token_hex(24)
            save_json(SECRETS_PATH, data)
        elif SECRETS_PATH.exists():
            os.chmod(SECRETS_PATH, 0o600)
        return data


def secret_value(name: str) -> str | None:
    # Environment is an explicit override for CI/tests and ephemeral shells.
    env_name = "PONYTAIL_GATEWAY_TOKEN" if name == "gatewayToken" else name
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    value = str(load_secrets().get(name) or "").strip()
    return value or None


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = load_json(path, {})
    if not isinstance(cfg, dict):
        raise ValueError("mobile.json must be a JSON object")
    providers = cfg.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("mobile.json must contain a non-empty providers array")
    seen = set()
    for p in providers:
        if not isinstance(p, dict):
            raise ValueError("each provider must be an object")
        pid = p.get("id")
        base = str(p.get("baseURL") or "").rstrip("/")
        key_env = p.get("keyEnv")
        models = p.get("models")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", pid):
            raise ValueError(f"invalid provider id: {pid!r}")
        if pid in seen:
            raise ValueError(f"duplicate provider id: {pid}")
        seen.add(pid)
        if not (
            base.startswith("https://")
            or base.startswith("http://127.0.0.1:")
            or base.startswith("http://localhost:")
        ):
            raise ValueError(f"provider {pid}: baseURL must be https:// (localhost allowed for tests)")
        if not isinstance(key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", key_env):
            raise ValueError(f"provider {pid}: invalid keyEnv")
        if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
            raise ValueError(f"provider {pid}: models must be a non-empty string array")
        if "priority" in p:
            float(p["priority"])
        if "timeoutSec" in p and not 5 <= float(p["timeoutSec"]) <= 900:
            raise ValueError(f"provider {pid}: timeoutSec must be 5..900")
    listen = str(cfg.get("listen") or "127.0.0.1")
    if listen not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("gateway may bind only to loopback")
    port = int(cfg.get("port") or 8766)
    if not 1024 <= port <= 65535:
        raise ValueError("port must be 1024..65535")
    max_concurrent = int(cfg.get("maxConcurrent") or 2)
    if not 1 <= max_concurrent <= 8:
        raise ValueError("maxConcurrent must be 1..8")
    cfg["maxConcurrent"] = max_concurrent
    return cfg


def state() -> dict:
    data = load_json(STATE_PATH, {"routes": {}, "providers": {}})
    if not isinstance(data, dict):
        return {"routes": {}, "providers": {}}
    data.setdefault("routes", {})
    data.setdefault("providers", {})
    return data


def save_state(st: dict) -> None:
    save_json(STATE_PATH, st)


def env_key(provider: dict) -> str | None:
    return secret_value(provider["keyEnv"])


def route_id(provider: dict, model: str) -> str:
    return provider["id"] + "/" + model


def retry_after(headers) -> int | None:
    try:
        value = headers.get("Retry-After")
        if value is None:
            return None
        return max(1, min(24 * 3600, int(float(value))))
    except Exception:
        return None


def rate_meta(headers) -> dict:
    out = {}
    for name in RATE_HEADERS:
        try:
            value = headers.get(name)
        except Exception:
            value = None
        if value is not None:
            out[name] = str(value)[:120]
    return out


def classify_http(code: int, body: str) -> str | None:
    text = body.lower()
    if code == 429 or "resource_exhausted" in text or "rate limit" in text or "quota" in text:
        return "quota"
    if code == 401:
        return "auth"
    if code == 403:
        if "model" in text and any(w in text for w in ("access", "available", "permission", "allowed", "not found")):
            return "missing"
        return "auth"
    if code == 404 or any(w in text for w in ("unknown model", "model not found", "no such model")):
        return "missing"
    if code == 400 and any(w in text for w in ("unsupported", "not supported", "does not support", "unknown field", "unrecognized")):
        return "incompatible"
    if code in (408, 425, 500, 502, 503, 504) or any(
        w in text for w in ("temporarily unavailable", "overloaded", "high demand", "timeout")
    ):
        return "transient"
    return None


def mark(
    ref: str,
    kind: str,
    elapsed_ms: int = 0,
    retry: int | None = None,
    headers=None,
) -> None:
    with LOCK:
        st = state()
        routes = st.setdefault("routes", {})
        providers = st.setdefault("providers", {})
        row = routes.setdefault(ref, {})
        pid = ref.split("/", 1)[0]
        now = int(time.time())
        if kind == "ok":
            row["ok"] = int(row.get("ok") or 0) + 1
            row["fail"] = max(0, int(row.get("fail") or 0) - 1)
            row["cooldownUntil"] = 0
            row["reason"] = "ok"
            row["lastOk"] = now
            if elapsed_ms:
                old = float(row.get("avgMs") or 0)
                row["avgMs"] = int(elapsed_ms if old <= 0 else old * 0.75 + elapsed_ms * 0.25)
            meta = rate_meta(headers) if headers is not None else {}
            if meta:
                row["rate"] = meta
            prow = providers.get(pid)
            if isinstance(prow, dict) and prow.get("reason") == "auth":
                providers.pop(pid, None)
        else:
            fails = int(row.get("fail") or 0) + 1
            row["fail"] = fails
            row["reason"] = kind
            row["lastFail"] = now
            if retry:
                delay = retry
            elif kind == "quota":
                delay = 3 * 3600
            elif kind == "missing":
                delay = 24 * 3600
            elif kind == "auth":
                delay = 3600
            elif kind == "incompatible":
                delay = 6 * 3600
            else:
                delay = min(15 * 60, 30 * (2 ** min(5, fails - 1)))
            row["cooldownUntil"] = now + delay
            if kind == "auth":
                providers[pid] = {"reason": "auth", "cooldownUntil": now + delay, "lastFail": now}
        if len(routes) > 250:
            oldest = sorted(
                routes,
                key=lambda k: max(routes[k].get("lastOk", 0), routes[k].get("lastFail", 0)),
            )
            for key in oldest[:-250]:
                routes.pop(key, None)
        save_state(st)


def openrouter_discovered_models(provider: dict) -> list[str]:
    if provider.get("freeGuard") != "openrouter":
        return []
    d = load_json(DISCOVERY_PATH, {})
    return list((d.get("providers") or {}).get(provider["id"], {}).get("freeModels") or [])


def route_defs(cfg: dict):
    for pi, provider in enumerate(cfg["providers"]):
        if not env_key(provider):
            continue
        models = list(provider.get("models") or [])
        for m in openrouter_discovered_models(provider):
            if m not in models:
                models.append(m)
        for mi, model in enumerate(models):
            if provider.get("freeGuard") == "openrouter":
                # Never auto-use paid OpenRouter IDs. `openrouter/free` and
                # explicit :free variants are the strict zero-price paths.
                if model != "openrouter/free" and not model.endswith(":free"):
                    continue
            yield pi, mi, provider, model


def route_score(pi: int, mi: int, provider: dict, row: dict) -> float:
    priority = float(provider.get("priority", 100 + pi * 10))
    score = 10000 - priority * 100 - mi
    score += min(100, int(row.get("ok") or 0) * 3)
    score -= min(500, int(row.get("fail") or 0) * 40)
    avg = float(row.get("avgMs") or 0)
    if avg:
        score -= min(120, avg / 250.0)
    return score


def candidates(cfg: dict) -> list[tuple[float, dict, str]]:
    st = state()
    routes_state = st.get("routes", {})
    provider_state = st.get("providers", {})
    now = time.time()
    rows: list[tuple[float, dict, str]] = []
    for pi, mi, provider, model in route_defs(cfg):
        ref = route_id(provider, model)
        row = routes_state.get(ref, {})
        prow = provider_state.get(provider["id"], {})
        if float(row.get("cooldownUntil") or 0) > now:
            continue
        if float(prow.get("cooldownUntil") or 0) > now:
            continue
        rows.append((route_score(pi, mi, provider, row), provider, model))
    rows.sort(key=lambda r: (-r[0], r[1]["id"], r[2]))
    return rows


def upstream_headers(provider: dict, key: str) -> dict[str, str]:
    out = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": USER_AGENT,
    }
    for name, value in (provider.get("headers") or {}).items():
        if isinstance(name, str) and isinstance(value, str) and "\n" not in name + value and "\r" not in name + value:
            out[name] = value
    return out


class PassthroughError(Exception):
    def __init__(self, code: int, headers, body: bytes, ref: str):
        super().__init__(f"upstream HTTP {code} via {ref}")
        self.code = code
        self.headers = headers
        self.body = body
        self.ref = ref


def choose_upstream(cfg: dict, endpoint: str, payload: dict):
    errors = []
    bad_providers = set()
    for _score, provider, model in candidates(cfg):
        if provider["id"] in bad_providers:
            continue
        key = env_key(provider)
        if not key:
            continue
        ref = route_id(provider, model)
        body = dict(payload)
        body["model"] = model
        data = json.dumps(body, separators=(",", ":")).encode()
        url = provider["baseURL"].rstrip("/") + "/" + endpoint.lstrip("/")
        req = urllib.request.Request(url, data=data, headers=upstream_headers(provider, key), method="POST")
        started = time.monotonic()
        try:
            response = urllib.request.urlopen(req, timeout=float(provider.get("timeoutSec") or 180))
            return response, ref, started
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            kind = classify_http(e.code, raw)
            errors.append({"route": ref, "status": e.code, "kind": kind, "body": raw[:500]})
            if kind is None:
                # A real request error should stay visible. Trying another model
                # can hide malformed tool schemas, invalid parameters, etc.
                raise PassthroughError(e.code, e.headers, raw.encode(), ref)
            mark(ref, kind, int((time.monotonic() - started) * 1000), retry_after(e.headers), e.headers)
            if kind == "auth":
                bad_providers.add(provider["id"])
            continue
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            errors.append({"route": ref, "kind": "transient", "body": str(e)[:300]})
            mark(ref, "transient", int((time.monotonic() - started) * 1000))
            continue
    raise RuntimeError("no healthy free route: " + json.dumps(errors[-6:], ensure_ascii=False))


def get_models(provider: dict) -> dict:
    key = env_key(provider)
    if not key:
        raise RuntimeError("missing " + provider["keyEnv"])
    url = provider["baseURL"].rstrip("/") + "/models"
    req = urllib.request.Request(url, headers=upstream_headers(provider, key), method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def coding_score(model: dict) -> int:
    text = " ".join(str(model.get(k) or "") for k in ("id", "name", "description")).lower()
    weights = {
        "code": 80,
        "coder": 100,
        "coding": 80,
        "software": 45,
        "agent": 40,
        "swe": 90,
        "flint": 100,
        "north": 70,
        "nemotron": 55,
        "deepseek": 55,
        "qwen": 45,
        "devstral": 90,
        "tool": 30,
    }
    return sum(v for k, v in weights.items() if k in text)


def discover(cfg: dict) -> dict:
    result = {"time": int(time.time()), "providers": {}}
    for p in cfg["providers"]:
        if not env_key(p):
            result["providers"][p["id"]] = {"ok": False, "error": "key not configured"}
            continue
        try:
            data = get_models(p)
            models = [m for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
            ids = {str(m["id"]) for m in models}
            configured_missing = [
                m for m in (p.get("models") or [])
                if m not in ids and not (p.get("freeGuard") == "openrouter" and m == "openrouter/free")
            ]
            row = {"ok": True, "count": len(models), "configuredMissing": configured_missing}
            if p.get("freeGuard") == "openrouter":
                free = [m for m in models if str(m["id"]).endswith(":free")]
                free.sort(key=lambda m: (-coding_score(m), str(m["id"])))
                row["freeModels"] = [m["id"] for m in free[:16]]
            result["providers"][p["id"]] = row
        except Exception as e:
            result["providers"][p["id"]] = {"ok": False, "error": str(e)[:300]}
    save_json(DISCOVERY_PATH, result)
    return result


def public_status(cfg: dict) -> dict:
    st = state()
    route_state = st.get("routes", {})
    provider_state = st.get("providers", {})
    now = int(time.time())
    routes = []
    usable = 0
    for pi, mi, provider, model in route_defs(cfg):
        ref = route_id(provider, model)
        row = route_state.get(ref, {})
        prow = provider_state.get(provider["id"], {})
        route_cd = max(0, int(float(row.get("cooldownUntil") or 0)) - now)
        provider_cd = max(0, int(float(prow.get("cooldownUntil") or 0)) - now)
        cooldown = max(route_cd, provider_cd)
        if not cooldown:
            usable += 1
        routes.append({
            "route": ref,
            "score": int(route_score(pi, mi, provider, row)),
            "ok": int(row.get("ok") or 0),
            "fail": int(row.get("fail") or 0),
            "avgMs": row.get("avgMs"),
            "reason": prow.get("reason") if provider_cd else row.get("reason"),
            "cooldownSec": cooldown,
            "rate": row.get("rate") or {},
        })
    routes.sort(key=lambda r: (r["cooldownSec"] > 0, -r["score"], r["route"]))
    missing = [p["id"] for p in cfg["providers"] if not env_key(p)]
    return {"ok": usable > 0, "routes": routes, "missingKeys": missing}


def gateway_token() -> str:
    return secret_value("gatewayToken") or ""


def local_url(cfg: dict, path: str) -> str:
    host = cfg.get("listen") or "127.0.0.1"
    if host == "localhost":
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    return f"http://{host}:{int(cfg.get('port') or 8766)}{path}"


def fetch_health(cfg: dict) -> dict:
    token = gateway_token()
    if not token:
        raise RuntimeError("gateway token is not configured")
    req = urllib.request.Request(
        local_url(cfg, "/health"),
        headers={"Authorization": "Bearer " + token, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PonytailMobile/2"

    def authorized(self) -> bool:
        expected = gateway_token()
        if not expected:
            return False
        got = self.headers.get("Authorization", "")
        return secrets_module.compare_digest(got, "Bearer " + expected)

    def json_out(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if not self.authorized():
            self.json_out(401, {"error": {"message": "invalid local gateway token"}})
            return
        cfg = self.server.cfg
        if self.path in ("/health", "/v1/health"):
            self.json_out(200, public_status(cfg))
            return
        if self.path in ("/models", "/v1/models"):
            self.json_out(200, {
                "object": "list",
                "data": [{"id": "free", "object": "model", "owned_by": "ponytail-mobile"}],
            })
            return
        self.json_out(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.authorized():
            self.json_out(401, {"error": {"message": "invalid local gateway token"}})
            return
        if self.path not in ("/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses"):
            self.json_out(404, {"error": {"message": "unsupported endpoint"}})
            return
        if not self.server.slots.acquire(timeout=15):
            self.json_out(503, {"error": {"message": "local gateway busy; retry shortly"}})
            return
        try:
            self._proxy_post()
        finally:
            self.server.slots.release()

    def _proxy_post(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 16 * 1024 * 1024:
                raise ValueError("request body must be 1..16MiB")
            payload = json.loads(self.rfile.read(n))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            if payload.get("model") not in (None, "free", "ponytail/free"):
                raise ValueError("gateway exposes only model free")
            endpoint = "responses" if self.path.endswith("/responses") else "chat/completions"
            upstream, ref, started = choose_upstream(self.server.cfg, endpoint, payload)
            self.send_response(getattr(upstream, "status", 200))
            for name in ("Content-Type", "Cache-Control", "X-Request-Id"):
                value = upstream.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("X-Ponytail-Route", ref)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                mark(ref, "ok", int((time.monotonic() - started) * 1000), headers=upstream.headers)
            except Exception:
                # The response may already be partially delivered, so replaying
                # it on another model could duplicate tool calls or text.
                mark(ref, "transient", int((time.monotonic() - started) * 1000))
            self.close_connection = True
        except PassthroughError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(e.body)))
            self.send_header("X-Ponytail-Route", e.ref)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(e.body)
            self.close_connection = True
        except Exception as e:
            self.json_out(503, {"error": {"message": str(e)}})

    def log_message(self, fmt, *args):
        if os.environ.get("PONYTAIL_GATEWAY_LOG"):
            super().log_message(fmt, *args)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, cfg):
        self.cfg = cfg
        self.slots = threading.BoundedSemaphore(int(cfg.get("maxConcurrent") or 2))
        super().__init__(address, handler)


def serve(cfg: dict) -> None:
    if not gateway_token():
        raise SystemExit("gateway token is not configured; run termux/install.sh")
    host = cfg.get("listen") or "127.0.0.1"
    port = int(cfg.get("port") or 8766)
    print(f"Ponytail Mobile gateway: http://{host}:{port}/v1", flush=True)
    Server((host, port), Handler, cfg).serve_forever()


def selftest() -> None:
    assert classify_http(429, "") == "quota"
    assert classify_http(401, "") == "auth"
    assert classify_http(404, "model not found") == "missing"
    assert classify_http(400, "this parameter is unsupported") == "incompatible"
    assert classify_http(400, "invalid JSON schema") is None

    class Mock(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.calls += 1
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n))
            if self.server.mode == "quota":
                raw = b'{"error":{"message":"rate limit"}}'
                self.send_response(429)
                self.send_header("Retry-After", "7")
            elif self.server.mode == "auth":
                raw = b'{"error":{"message":"invalid api key"}}'
                self.send_response(401)
            else:
                raw = json.dumps({"model": body.get("model"), "choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Remaining-Requests", "999")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    old_state = globals()["STATE_PATH"]
    old_disc = globals()["DISCOVERY_PATH"]
    old_secrets = globals()["SECRETS_PATH"]
    with tempfile.TemporaryDirectory() as td:
        globals()["STATE_PATH"] = Path(td) / "state.json"
        globals()["DISCOVERY_PATH"] = Path(td) / "discovery.json"
        globals()["SECRETS_PATH"] = Path(td) / "secrets.json"
        save_json(globals()["SECRETS_PATH"], {
            "gatewayToken": "local",
            "TEST_KEY_A": "a",
            "TEST_KEY_B": "b",
        })
        servers = []
        try:
            for mode in ("quota", "ok"):
                s = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
                s.mode = mode
                s.calls = 0
                threading.Thread(target=s.serve_forever, daemon=True).start()
                servers.append(s)
            cfg = {
                "listen": "127.0.0.1",
                "port": 8766,
                "maxConcurrent": 2,
                "providers": [
                    {"id": "a", "baseURL": f"http://127.0.0.1:{servers[0].server_port}/v1", "keyEnv": "TEST_KEY_A", "models": ["m-a"], "priority": 1},
                    {"id": "b", "baseURL": f"http://127.0.0.1:{servers[1].server_port}/v1", "keyEnv": "TEST_KEY_B", "models": ["m-b"], "priority": 2},
                ],
            }
            response, ref, started = choose_upstream(cfg, "chat/completions", {"model": "free", "messages": []})
            data = json.loads(response.read())
            mark(ref, "ok", int((time.monotonic() - started) * 1000), headers=response.headers)
            assert ref == "b/m-b", ref
            assert data["model"] == "m-b"
            st = state()["routes"]
            assert st["a/m-a"]["reason"] == "quota"
            assert 1 <= st["a/m-a"]["cooldownUntil"] - int(time.time()) <= 7
            assert st["b/m-b"]["ok"] == 1
            assert st["b/m-b"]["rate"]["x-ratelimit-remaining-requests"] == "999"

            # Auth is provider-wide: one bad key must not burn every sibling model.
            auth = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
            auth.mode = "auth"
            auth.calls = 0
            threading.Thread(target=auth.serve_forever, daemon=True).start()
            servers.append(auth)
            save_state({"routes": {}, "providers": {}})
            cfg2 = {
                "listen": "127.0.0.1",
                "port": 8766,
                "providers": [
                    {"id": "a", "baseURL": f"http://127.0.0.1:{auth.server_port}/v1", "keyEnv": "TEST_KEY_A", "models": ["m1", "m2"], "priority": 1},
                    {"id": "b", "baseURL": f"http://127.0.0.1:{servers[1].server_port}/v1", "keyEnv": "TEST_KEY_B", "models": ["m-b"], "priority": 2},
                ],
            }
            response, ref, _ = choose_upstream(cfg2, "chat/completions", {"model": "free", "messages": []})
            response.read()
            assert ref == "b/m-b"
            assert auth.calls == 1, auth.calls
            assert state()["providers"]["a"]["reason"] == "auth"
        finally:
            for s in servers:
                s.shutdown()
                s.server_close()
            globals()["STATE_PATH"] = old_state
            globals()["DISCOVERY_PATH"] = old_disc
            globals()["SECRETS_PATH"] = old_secrets
    print("OK: ponytail mobile gateway selftest")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ponytail Mobile free API gateway")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=("serve", "status", "health", "discover", "selftest", "ensure-secrets", "set-secret", "token"),
    )
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--name")
    args = parser.parse_args()
    if args.command == "selftest":
        selftest()
        return
    if args.command == "ensure-secrets":
        ensure_secrets()
        return
    if args.command == "set-secret":
        if not args.name:
            raise SystemExit("--name is required")
        value = sys.stdin.read().rstrip("\n")
        if not value:
            raise SystemExit("secret is empty")
        save_secret(args.name, value)
        return
    if args.command == "token":
        print(ensure_secrets()["gatewayToken"])
        return
    cfg = load_config(Path(args.config))
    if args.command == "serve":
        serve(cfg)
    elif args.command == "discover":
        print(json.dumps(discover(cfg), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(public_status(cfg), ensure_ascii=False, indent=2))
    elif args.command == "health":
        print(json.dumps(fetch_health(cfg), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
