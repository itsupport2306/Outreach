import os
import time
import random
import socket
import http.client
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import urllib3


class CeipalClient:
    def __init__(
        self,
        email: str,
        password: str,
        api_key: str,
        auth_url: str = "https://api.ceipal.com/v1/createAuthtoken/",
    ) -> None:
        self.email = email
        self.password = password
        self.api_key = api_key
        self.auth_url = auth_url

        self._http = requests.Session()
        self._token: Optional[str] = None
        self._token_set_at: float = 0.0

    def _get_token(self) -> str:
        if self._token:
            return self._token

        attempts = [
            ("data", "body", {"email": self.email, "password": self.password, "api_key": self.api_key, "json": 1}),
            ("data", "body", {"email": self.email, "password": self.password, "apikey": self.api_key, "json": 1}),
            ("json", "body", {"email": self.email, "password": self.password, "api_key": self.api_key, "json": 1}),
            ("json", "body", {"email": self.email, "password": self.password, "apikey": self.api_key, "json": 1}),
            ("data", "params", {"email": self.email, "password": self.password, "api_key": self.api_key, "json": 1}),
            ("data", "params", {"email": self.email, "password": self.password, "apikey": self.api_key, "json": 1}),
        ]

        last_exc: Optional[Exception] = None
        for mode, where, payload in attempts:
            try:
                if mode == "json":
                    r = self._http.post(
                        self.auth_url,
                        params=payload if where == "params" else None,
                        json=payload if where == "body" else None,
                        timeout=20,
                    )
                else:
                    r = self._http.post(
                        self.auth_url,
                        params=payload if where == "params" else None,
                        data=payload if where == "body" else None,
                        timeout=20,
                    )
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                last_exc = e
                # Retry transient network issues briefly during auth.
                if isinstance(
                    e,
                    (
                        requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError,
                        http.client.RemoteDisconnected,
                        socket.timeout,
                        urllib3.exceptions.ProtocolError,
                    ),
                ):
                    time.sleep(0.75 + random.random() * 0.75)
                if isinstance(e, requests.HTTPError) and hasattr(e, "response") and e.response is not None:
                    resp = e.response
                    snippet = self._safe_snippet(resp.text)
                    last_exc = RuntimeError(
                        f"CEIPAL auth failed: status={resp.status_code}, url={self.auth_url}, body={snippet}"
                    )
                data = None

        if not data:
            raise RuntimeError("CEIPAL auth failed") from last_exc

        def _extract_token(obj: Any) -> Optional[str]:
            if isinstance(obj, dict):
                direct = (
                    obj.get("token")
                    or obj.get("authtoken")
                    or obj.get("authToken")
                    or obj.get("AuthToken")
                    or obj.get("access_token")
                    or obj.get("accessToken")
                )
                if isinstance(direct, str) and direct.strip():
                    return direct.strip()

                for k in ("data", "result", "response", "auth", "payload"):
                    if k in obj:
                        nested = _extract_token(obj.get(k))
                        if nested:
                            return nested

                for v in obj.values():
                    nested = _extract_token(v)
                    if nested:
                        return nested

            if isinstance(obj, list):
                for item in obj:
                    nested = _extract_token(item)
                    if nested:
                        return nested

            return None

        token = _extract_token(data)
        if not token:
            snippet = self._safe_snippet(str(data))
            keys = list(data.keys()) if isinstance(data, dict) else []
            raise RuntimeError(
                f"CEIPAL auth token not found in response; keys={keys}; body={snippet}"
            )

        self._token = token
        self._token_set_at = time.time()
        return token

    def _headers(self) -> Dict[str, str]:
        token = self._get_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _headers_raw_token(self) -> Dict[str, str]:
        token = self._get_token()
        return {"Authorization": token, "Accept": "application/json"}

    @staticmethod
    def _safe_snippet(text: str, limit: int = 500) -> str:
        text = (text or "").replace("\r", " ").replace("\n", " ").strip()
        if len(text) > limit:
            return text[:limit] + "..."
        return text

    @staticmethod
    def _with_query_params(url: str, params: Dict[str, str]) -> str:
        parts = urlsplit(url)
        existing = dict(parse_qsl(parts.query, keep_blank_values=True))
        existing.update({k: v for k, v in params.items() if v is not None and v != ""})
        new_query = urlencode(existing, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    def get_report_data(self, report_url: str) -> Any:
        last_exc: Optional[Exception] = None

        # Some CEIPAL BI report endpoints expect the token in the querystring.
        token = self._get_token()
        report_url_with_token = self._with_query_params(
            report_url,
            {"authtoken": token, "api_key": self.api_key},
        )

        for headers_fn in (self._headers, self._headers_raw_token):
            try:
                attempts = int(os.getenv("CEIPAL_REPORT_MAX_RETRIES", "4") or "4")
            except Exception:
                attempts = 4
            attempts = max(0, min(attempts, 12))

            base_sleep = float(os.getenv("CEIPAL_REPORT_RETRY_BASE_SECONDS", "1.5") or "1.5")
            base_sleep = max(0.1, min(base_sleep, 30.0))

            jitter_max = float(os.getenv("CEIPAL_REPORT_RETRY_JITTER_SECONDS", "0.4") or "0.4")
            jitter_max = max(0.0, min(jitter_max, 5.0))

            def _sleep_for_attempt(attempt_num: int, extra: float = 0.0) -> None:
                sleep_s = base_sleep * (2 ** max(0, int(attempt_num)))
                sleep_s = min(sleep_s, 120.0)
                sleep_s = sleep_s + (random.random() * jitter_max) + float(extra or 0.0)
                time.sleep(max(0.25, sleep_s))

            for attempt in range(attempts + 1):
                try:
                    r = self._http.get(report_url, headers=headers_fn(), timeout=60)
                    if r.status_code == 405:
                        r = self._http.post(report_url, headers=headers_fn(), timeout=60)
                    if r.status_code in (401, 403):
                        self._token = None
                        r = self._http.get(report_url, headers=headers_fn(), timeout=60)
                        if r.status_code == 405:
                            r = self._http.post(report_url, headers=headers_fn(), timeout=60)

                    # If BI endpoint rejects auth headers, retry with querystring token.
                    if r.status_code == 400 and "Invalid Credentials" in (r.text or ""):
                        r = self._http.get(report_url_with_token, headers=headers_fn(), timeout=60)
                        if r.status_code == 405:
                            r = self._http.post(report_url_with_token, headers=headers_fn(), timeout=60)

                    # Rate limit: backoff and retry.
                    if r.status_code == 429 and attempt < attempts:
                        retry_after = (r.headers.get("Retry-After") or "").strip()
                        sleep_s = None
                        if retry_after:
                            try:
                                sleep_s = float(retry_after)
                            except Exception:
                                sleep_s = None
                        if sleep_s is None:
                            _sleep_for_attempt(attempt)
                        else:
                            time.sleep(max(0.25, min(float(sleep_s), 120.0)))
                        continue

                    r.raise_for_status()
                    return r.json()
                except Exception as e:
                    last_exc = e
                    # Transient network errors (RemoteDisconnected, connection reset, timeouts)
                    # should be retried with backoff.
                    if attempt < attempts and isinstance(
                        e,
                        (
                            requests.exceptions.Timeout,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.ChunkedEncodingError,
                            http.client.RemoteDisconnected,
                            socket.timeout,
                            urllib3.exceptions.ProtocolError,
                        ),
                    ):
                        _sleep_for_attempt(attempt)
                        continue
                    if isinstance(e, requests.HTTPError) and hasattr(e, "response") and e.response is not None:
                        resp = e.response
                        if resp.status_code == 429 and attempt < attempts:
                            retry_after = (resp.headers.get("Retry-After") or "").strip()
                            sleep_s = None
                            if retry_after:
                                try:
                                    sleep_s = float(retry_after)
                                except Exception:
                                    sleep_s = None
                            if sleep_s is None:
                                _sleep_for_attempt(attempt)
                            else:
                                time.sleep(max(0.25, min(float(sleep_s), 120.0)))
                            continue
                        snippet = self._safe_snippet(resp.text)
                        raise RuntimeError(
                            f"CEIPAL report request failed: status={resp.status_code}, url={report_url}, body={snippet}"
                        ) from e
                    raise

        raise RuntimeError("CEIPAL report request failed") from last_exc


def build_ceipal_client_from_env() -> CeipalClient:
    email = (os.getenv("CEIPAL_EMAIL") or "").strip()
    password = (os.getenv("CEIPAL_PASSWORD") or "").strip()
    api_key = (os.getenv("CEIPAL_API_KEY") or "").strip()

    if not email or not password or not api_key:
        raise ValueError("CEIPAL_EMAIL/CEIPAL_PASSWORD/CEIPAL_API_KEY must be set")

    auth_url = (os.getenv("CEIPAL_AUTH_URL") or "https://api.ceipal.com/v1/createAuthtoken/").strip()
    return CeipalClient(email=email, password=password, api_key=api_key, auth_url=auth_url)
