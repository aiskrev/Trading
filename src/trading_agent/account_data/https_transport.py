"""GET-only HTTPS transport implementing account_data.http.HttpTransport.

One fixed host per instance, chosen from an allow-list by the vendor adapter; system trust store with
hostname verification; TLS >= 1.2; no proxies, no redirects followed (a 3xx is just a non-200 status);
response size capped. There is no method for POST/PUT/PATCH/DELETE.
"""
import http.client
import ssl
from typing import Dict

from trading_agent.account_data.http import HttpResponse

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class HttpsGetTransport:
    def __init__(self, host: str, allowed_hosts: frozenset) -> None:
        if host not in allowed_hosts:
            raise ValueError(f"host not allowed: {host!r}")
        self._host = host
        self._ctx = ssl.create_default_context()
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def get(self, path: str, headers: Dict[str, str], timeout_s: float) -> HttpResponse:
        if not path.startswith("/") or "\r" in path or "\n" in path or " " in path:
            raise ValueError("bad path")
        conn = http.client.HTTPSConnection(self._host, 443, timeout=timeout_s, context=self._ctx)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise OSError("response too large")
            return HttpResponse(resp.status, {k: v for k, v in resp.getheaders()}, body)
        except http.client.HTTPException as exc:
            raise OSError(type(exc).__name__) from None
        finally:
            conn.close()
