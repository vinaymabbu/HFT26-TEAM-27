"""Durable object storage for serverless hosting (Cloudflare R2, or any S3-compatible bucket).

A serverless function has no disk that survives: every cold start begins empty and two requests may run on different
machines. The object store is where evidence and results live between requests (see mirror.py), and where the browser
uploads large videos directly (presigned URLs), because a function cannot receive more than a few MB in one request.

Only what is needed, with no SDK (an AWS SDK would add ~70 MB to a function bundle that is already near its limit):
get, put, delete, list, and a presigned PUT, all signed with AWS Signature Version 4.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

TIMEOUT_S = 120.0


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str


# ------------------------------------------------------------------ AWS Signature Version 4

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    return _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), date), region), service), "aws4_request")


def _path(path: str) -> str:
    return quote(path, safe="/-_.~")


def _query(params: dict[str, str]) -> str:
    return "&".join(f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(params.items()))


def presign_url(method: str, host: str, path: str, access_key: str, secret: str, region: str, *, service: str = "s3",
                expires: int = 3600, now: datetime | None = None, scheme: str = "https",
                params: dict[str, str] | None = None) -> str:
    """A URL that lets whoever holds it perform `method` on `path` until it expires (the body is not signed)."""
    now = now or datetime.now(timezone.utc)
    stamp, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{date}/{region}/{service}/aws4_request"
    q = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access_key}/{scope}", "X-Amz-Date": stamp,
         "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host", **(params or {})}
    canonical = f"{method}\n{_path(path)}\n{_query(q)}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
    to_sign = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signature = hmac.new(_signing_key(secret, date, region, service), to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{_path(path)}?{_query(q)}&X-Amz-Signature={signature}"


def sign_headers(method: str, host: str, path: str, query: dict[str, str], payload_sha256: str, access_key: str,
                 secret: str, region: str, *, service: str = "s3", now: datetime | None = None,
                 extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers (including Authorization) for a request signed with SigV4."""
    now = now or datetime.now(timezone.utc)
    stamp, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": payload_sha256, "x-amz-date": stamp,
               **{k.lower(): v for k, v in (extra or {}).items()}}
    names = ";".join(sorted(headers))
    canonical = (f"{method}\n{_path(path)}\n{_query(query)}\n"
                 + "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers)) + f"\n{names}\n{payload_sha256}")
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signature = hmac.new(_signing_key(secret, date, region, service), to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={names}, Signature={signature}"
    return headers


# ------------------------------------------------------------------ stores

class R2ObjectStore:
    """A bucket on Cloudflare R2 (path-style S3 API, region "auto")."""

    def __init__(self, account_id: str, bucket: str, access_key: str, secret: str, endpoint: str | None = None):
        self.host = endpoint or f"{account_id}.r2.cloudflarestorage.com"
        self.bucket, self.access_key, self.secret, self.region = bucket, access_key, secret, "auto"
        self._http = httpx.Client(timeout=TIMEOUT_S)

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None, body: bytes = b"",
                 extra: dict[str, str] | None = None) -> httpx.Response:
        path = f"/{self.bucket}/{key}" if key else f"/{self.bucket}"
        headers = sign_headers(method, self.host, path, query or {}, hashlib.sha256(body).hexdigest(),
                               self.access_key, self.secret, self.region, extra=extra)
        url = f"https://{self.host}{_path(path)}" + (f"?{_query(query)}" if query else "")
        r = self._http.request(method, url, content=body or None, headers={k: v for k, v in headers.items() if k != "host"})
        if r.status_code >= 400 and r.status_code != 404:
            raise RuntimeError(f"object store {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    def get(self, key: str) -> bytes | None:
        r = self._request("GET", key)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content

    def put(self, key: str, data: bytes) -> str:
        r = self._request("PUT", key, body=data)
        r.raise_for_status()
        return r.headers.get("etag", "").strip('"')

    def delete(self, key: str) -> None:
        r = self._request("DELETE", key)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out, token = [], None
        while True:
            q = {"list-type": "2", "prefix": prefix, **({"continuation-token": token} if token else {})}
            r = self._request("GET", query=q)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ns = {"s": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
            find = (lambda el, tag: el.findall(f"s:{tag}", ns)) if ns else (lambda el, tag: el.findall(tag))
            for c in find(root, "Contents"):
                get = lambda tag: (find(c, tag)[0].text or "")  # noqa: E731
                out.append(ObjectInfo(get("Key"), int(get("Size") or 0), get("ETag").strip('"')))
            more = find(root, "IsTruncated")
            token = (find(root, "NextContinuationToken") or [None])[0]
            if not (more and more[0].text == "true" and token is not None and token.text):
                return out
            token = token.text

    def presign_put(self, key: str, expires: int = 3600) -> str:
        return presign_url("PUT", self.host, f"/{self.bucket}/{key}", self.access_key, self.secret, self.region, expires=expires)


class LocalObjectStore:
    """A folder standing in for a bucket (tests and development); same interface as R2ObjectStore."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("bad key")
        return p

    def get(self, key: str) -> bytes | None:
        p = self._p(key)
        return p.read_bytes() if p.is_file() else None

    def put(self, key: str, data: bytes) -> str:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return hashlib.md5(data).hexdigest()

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out = []
        for p in sorted(self.root.rglob("*")) if self.root.exists() else []:
            key = p.relative_to(self.root).as_posix()
            if p.is_file() and key.startswith(prefix):
                out.append(ObjectInfo(key, p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest()))
        return out

    def presign_put(self, key: str, expires: int = 3600) -> str:
        return f"local://{key}"


def from_env():
    """R2 when R2_ACCOUNT_ID / R2_BUCKET / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are set, else None (local disk only)."""
    env = {k: v.strip().strip("\"'").strip() for k, v in os.environ.items() if k.startswith("R2_")}  # pasted values often carry whitespace or quotes
    if all(env.get(k) for k in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")):
        return R2ObjectStore(env["R2_ACCOUNT_ID"], env["R2_BUCKET"], env["R2_ACCESS_KEY_ID"], env["R2_SECRET_ACCESS_KEY"],
                             env.get("R2_ENDPOINT") or None)
    return None
