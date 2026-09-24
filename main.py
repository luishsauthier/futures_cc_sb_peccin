import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("futures-api")


BARCHART_API_URL = os.getenv(
    "BARCHART_API_URL",
    "https://ondemand.websol.barchart.com/getQuote.json",
).strip()
REQUEST_TIMEOUT = (5, 30)
MAX_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw_value, default)
        return default
    return max(value, minimum)


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name, "true" if default else "false")
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def parse_supported_roots() -> tuple[str, ...]:
    raw_value = os.getenv("SUPPORTED_ROOTS", "CC,SB")
    roots = [
        root.strip().upper()
        for root in raw_value.split(",")
        if root.strip()
    ]
    valid = [root for root in roots if re.fullmatch(r"[A-Z0-9]{1,8}", root)]
    return tuple(dict.fromkeys(valid or ["CC", "SB"]))


SUPPORTED_ROOTS = parse_supported_roots()
OFFICIAL_CACHE_TTL_SECONDS = env_int("BARCHART_CACHE_TTL_SECONDS", 60)
BRIDGE_MAX_AGE_SECONDS = env_int("BARCHART_BRIDGE_MAX_AGE_SECONDS", 900)
BRIDGE_FILE = Path(
    os.getenv("BARCHART_BRIDGE_FILE", "/tmp/barchart-bridge.json").strip()
)
ENABLE_BRIDGE_FALLBACK = env_bool("BARCHART_ENABLE_BRIDGE_FALLBACK", True)

OUTPUT_COLUMNS = [
    "Root",
    "Contract",
    "Last",
    "Change",
    "Open",
    "High",
    "Low",
    "Previous",
    "Volume",
    "Open_Int",
    "Time",
]

_official_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_official_cache_lock = threading.Lock()
_bridge_lock = threading.Lock()
_bridge_state: dict[str, Any] | None = None


api = FastAPI(title="Futures API", version="4.0.0")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_api_key() -> str:
    return os.getenv("BARCHART_API_KEY", "").strip()


def get_bridge_token() -> str:
    return os.getenv("BARCHART_BRIDGE_TOKEN", "").strip()


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return None if number is None else int(number)


def clean_time(value: Any) -> str | int | float | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    return str(value)


FUTURES_MONTH_CODES = "FGHJKMNQUVXZ"


def normalize_contract(value: Any, root: str) -> str | None:
    if value is None:
        return None

    text = str(value).strip().upper()
    if not text:
        return None

    # The browser endpoint may return either a plain symbol (CCZ26) or a
    # descriptive value containing the symbol. Extract only an outright
    # futures contract and ignore cash rows such as CCY00.
    pattern = re.compile(
        rf"(?<![A-Z0-9]){re.escape(root)}([{FUTURES_MONTH_CODES}])(\d{{1,4}})(?![A-Z0-9])"
    )
    match = pattern.search(text)
    if match is None:
        return None

    month_code, year_text = match.groups()

    if len(year_text) == 1:
        current_year = now_utc().year
        candidate = (current_year // 10) * 10 + int(year_text)
        if candidate < current_year - 2:
            candidate += 10
        year_suffix = f"{candidate % 100:02d}"
    elif len(year_text) == 2:
        year_suffix = year_text
    elif len(year_text) == 4:
        year_suffix = year_text[-2:]
    else:
        return None

    return f"{root}{month_code}{year_suffix}"


def normalize_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    root = str(raw.get("Root", "")).strip().upper()
    if root not in SUPPORTED_ROOTS:
        return None

    contract = normalize_contract(raw.get("Contract"), root)
    if contract is None:
        return None

    return {
        "Root": root,
        "Contract": contract,
        "Last": to_float(raw.get("Last")),
        "Change": to_float(raw.get("Change")),
        "Open": to_float(raw.get("Open")),
        "High": to_float(raw.get("High")),
        "Low": to_float(raw.get("Low")),
        "Previous": to_float(raw.get("Previous")),
        "Volume": to_int(raw.get("Volume")),
        "Open_Int": to_int(raw.get("Open_Int")),
        "Time": clean_time(raw.get("Time")),
    }


def load_bridge_file() -> None:
    global _bridge_state
    try:
        if not BRIDGE_FILE.exists():
            return
        body = json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
        captured_at = parse_datetime(body.get("capturedAt"))
        rows = [normalize_row(item) for item in body.get("data", [])]
        clean_rows = [item for item in rows if item is not None]
        if captured_at and clean_rows:
            _bridge_state = {
                "source": "Barchart Web",
                "capturedAt": iso_utc(captured_at),
                "data": clean_rows,
            }
            logger.info("Loaded bridge cache rows=%s", len(clean_rows))
    except Exception as exc:
        logger.warning("Could not load bridge cache: %s", exc)


def persist_bridge_state(state: dict[str, Any]) -> None:
    try:
        BRIDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = BRIDGE_FILE.with_suffix(BRIDGE_FILE.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(BRIDGE_FILE)
    except Exception as exc:
        logger.warning("Could not persist bridge cache: %s", exc)


def bridge_snapshot() -> tuple[dict[str, Any] | None, float | None]:
    with _bridge_lock:
        if _bridge_state is None:
            return None, None
        state = json.loads(json.dumps(_bridge_state))

    captured_at = parse_datetime(state.get("capturedAt"))
    if captured_at is None:
        return state, None
    age = max(0.0, (now_utc() - captured_at).total_seconds())
    return state, age


def bridge_rows_for_roots(
    roots: list[str],
    require_fresh: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str | None]:
    state, age = bridge_snapshot()
    if state is None:
        return [], [
            {"root": root, "error": "No Barchart browser data received yet"}
            for root in roots
        ], None

    captured_at = state.get("capturedAt")
    if require_fresh and (age is None or age > BRIDGE_MAX_AGE_SECONDS):
        return [], [
            {
                "root": root,
                "error": (
                    "Barchart browser data is stale; "
                    f"ageSeconds={None if age is None else round(age)}"
                ),
            }
            for root in roots
        ], captured_at

    data = state.get("data") or []
    selected = [item for item in data if item.get("Root") in roots]
    present = {item.get("Root") for item in selected}
    errors = [
        {"root": root, "error": "No contracts received for this root"}
        for root in roots
        if root not in present
    ]
    return selected, errors, captured_at


def validate_bridge_token(received: str | None) -> None:
    expected = get_bridge_token()
    if not expected or not received:
        raise HTTPException(status_code=404)
    if not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=404)


def get_official_cache(root: str) -> list[dict[str, Any]] | None:
    if OFFICIAL_CACHE_TTL_SECONDS <= 0:
        return None
    with _official_cache_lock:
        cached = _official_cache.get(root)
        if cached is None:
            return None
        created_at, rows = cached
        if time.monotonic() - created_at >= OFFICIAL_CACHE_TTL_SECONDS:
            _official_cache.pop(root, None)
            return None
        return json.loads(json.dumps(rows))


def set_official_cache(root: str, rows: list[dict[str, Any]]) -> None:
    if OFFICIAL_CACHE_TTL_SECONDS <= 0:
        return
    with _official_cache_lock:
        _official_cache[root] = (time.monotonic(), json.loads(json.dumps(rows)))


def request_official(root: str) -> dict[str, Any]:
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("Official Barchart credential is not configured")

    payload = {
        "apikey": api_key,
        "symbols": f"{root}^F",
        "fields": "openInterest,previousClose",
    }
    response: requests.Response | None = None
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                BARCHART_API_URL,
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "futures-cc-sb-peccin/4.0",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt < MAX_ATTEMPTS
            ):
                time.sleep(2 ** (attempt - 1))
                continue
            break
        except (requests.Timeout, requests.RequestException) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))
                continue
            raise RuntimeError(f"Official Barchart request failed for {root}: {exc}") from exc

    if response is None:
        raise RuntimeError(f"Official Barchart returned no response: {last_error}")

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Official Barchart returned non-JSON content; HTTP={response.status_code}"
        ) from exc

    api_status = body.get("status") or {}
    api_code = api_status.get("code")
    api_message = api_status.get("message", "Unknown error")
    if response.status_code >= 400 or str(api_code) != "200":
        raise RuntimeError(
            "Official Barchart rejected the request; "
            f"HTTP={response.status_code}; code={api_code}; message={api_message}"
        )
    return body


def official_rows(root: str, force_refresh: bool = False) -> list[dict[str, Any]]:
    if not force_refresh:
        cached = get_official_cache(root)
        if cached is not None:
            return cached

    results = request_official(root).get("results") or []
    rows: list[dict[str, Any]] = []

    for item in results:
        last = to_float(item.get("lastPrice"))
        change = to_float(item.get("netChange"))
        previous = to_float(item.get("previousClose"))
        if previous is None and last is not None and change is not None:
            previous = last - change

        row = normalize_row(
            {
                "Root": root,
                "Contract": item.get("symbol"),
                "Last": last,
                "Change": change,
                "Open": item.get("open"),
                "High": item.get("high"),
                "Low": item.get("low"),
                "Previous": previous,
                "Volume": item.get("volume"),
                "Open_Int": item.get("openInterest", item.get("previousOpenInterest")),
                "Time": item.get("tradeTimestamp", item.get("serverTimestamp")),
            }
        )
        if row is not None:
            rows.append(row)

    if not rows:
        raise RuntimeError(f"Official Barchart returned no contracts for {root}")

    set_official_cache(root, rows)
    return rows


def validate_requested_roots(roots: str) -> list[str]:
    root_list = [root.strip().upper() for root in roots.split(",") if root.strip()]
    root_list = list(dict.fromkeys(root_list))
    if not root_list:
        raise HTTPException(status_code=400, detail="Provide at least one root")
    if len(root_list) > 10:
        raise HTTPException(status_code=400, detail="The limit is 10 roots per request")

    invalid = [root for root in root_list if not re.fullmatch(r"[A-Z0-9]{1,8}", root)]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail={"message": "Invalid roots", "roots": invalid},
        )

    unsupported = [root for root in root_list if root not in SUPPORTED_ROOTS]
    if unsupported:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Unsupported roots",
                "roots": unsupported,
                "supportedRoots": list(SUPPORTED_ROOTS),
            },
        )
    return root_list


@api.get("/")
def read_root() -> dict[str, Any]:
    state, age = bridge_snapshot()
    return {
        "status": "ok",
        "message": "Futures API is online",
        "sourcePriority": ["Barchart OnDemand", "Barchart Web Browser Bridge"],
        "apiKeyConfigured": bool(get_api_key()),
        "bridgeConfigured": bool(get_bridge_token()),
        "bridgeHasData": bool(state and state.get("data")),
        "bridgeAgeSeconds": None if age is None else round(age),
        "bridgeMaxAgeSeconds": BRIDGE_MAX_AGE_SECONDS,
        "supportedRoots": list(SUPPORTED_ROOTS),
        "endpoints": ["/ping", "/futures?roots=CC,SB", "/bridge/status"],
    }


@api.head("/")
def read_root_head() -> Response:
    return Response(status_code=200)


@api.get("/ping")
def ping() -> dict[str, str]:
    return {"ping": "pong"}


@api.head("/ping")
def ping_head() -> Response:
    return Response(status_code=200)


@api.get("/bridge/status")
def bridge_status() -> dict[str, Any]:
    state, age = bridge_snapshot()
    rows = len(state.get("data", [])) if state else 0
    return {
        "configured": bool(get_bridge_token()),
        "hasData": rows > 0,
        "rows": rows,
        "capturedAt": None if state is None else state.get("capturedAt"),
        "ageSeconds": None if age is None else round(age),
        "maxAgeSeconds": BRIDGE_MAX_AGE_SECONDS,
        "fresh": age is not None and age <= BRIDGE_MAX_AGE_SECONDS,
    }


@api.post("/bridge/ingest", include_in_schema=False)
def bridge_ingest(
    payload: dict[str, Any],
    x_bridge_token: str | None = Header(default=None, alias="X-Bridge-Token"),
) -> dict[str, Any]:
    global _bridge_state
    validate_bridge_token(x_bridge_token)

    if payload.get("source") != "Barchart Web":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_BRIDGE_SOURCE",
                "message": "Invalid bridge source",
            },
        )

    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list) or not raw_rows or len(raw_rows) > 500:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_BRIDGE_DATA",
                "message": "Bridge data must contain between 1 and 500 rows",
                "receivedRows": len(raw_rows) if isinstance(raw_rows, list) else None,
            },
        )

    rows = [normalize_row(item) for item in raw_rows]
    clean_rows = [item for item in rows if item is not None]
    if not clean_rows:
        examples = []
        for item in raw_rows[:5]:
            if isinstance(item, dict):
                examples.append(
                    {
                        "Root": item.get("Root"),
                        "Contract": item.get("Contract"),
                    }
                )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_VALID_FUTURES_ROWS",
                "message": "No valid CC/SB futures rows were received",
                "receivedRows": len(raw_rows),
                "examples": examples,
            },
        )

    # Freshness is based on receipt time at the server. This avoids false
    # rejections caused by a workstation clock or timezone configuration.
    received_at = now_utc()
    client_captured_at = parse_datetime(payload.get("capturedAt"))

    state = {
        "source": "Barchart Web",
        "capturedAt": iso_utc(received_at),
        "clientCapturedAt": (
            None if client_captured_at is None else iso_utc(client_captured_at)
        ),
        "data": clean_rows,
    }

    with _bridge_lock:
        _bridge_state = state
    persist_bridge_state(state)

    logger.info(
        "Barchart browser bridge updated rows=%s roots=%s rejectedRows=%s",
        len(clean_rows),
        sorted({item["Root"] for item in clean_rows}),
        len(raw_rows) - len(clean_rows),
    )
    return {
        "status": "ok",
        "capturedAt": state["capturedAt"],
        "rows": len(clean_rows),
        "rejectedRows": len(raw_rows) - len(clean_rows),
        "roots": sorted({item["Root"] for item in clean_rows}),
    }


@api.get("/futures")
def read_futures(roots: str = "CC,SB", refresh: bool = False) -> dict[str, Any]:
    root_list = validate_requested_roots(roots)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    source_used: str | None = None

    if get_api_key():
        for root in root_list:
            try:
                rows.extend(official_rows(root, force_refresh=refresh))
            except Exception as exc:
                logger.exception("Official Barchart failed for root=%s", root)
                errors.append({"root": root, "error": str(exc)})
        if rows:
            source_used = "Barchart OnDemand"

    missing_roots = [
        root for root in root_list if not any(item["Root"] == root for item in rows)
    ]

    if missing_roots and (not get_api_key() or ENABLE_BRIDGE_FALLBACK):
        bridge_rows, bridge_errors, captured_at = bridge_rows_for_roots(missing_roots)
        if bridge_rows:
            rows.extend(bridge_rows)
            source_used = (
                "Barchart Web Browser Bridge"
                if source_used is None
                else "Barchart OnDemand + Browser Bridge"
            )
            failed_bridge_roots = {item["root"] for item in bridge_errors}
            errors = [item for item in errors if item["root"] in failed_bridge_roots]
        else:
            errors.extend(
                item for item in bridge_errors if item not in errors
            )

    if not rows:
        state, age = bridge_snapshot()
        raise HTTPException(
            status_code=503,
            detail={
                "code": "FUTURES_SOURCE_UNAVAILABLE",
                "message": "Barchart prices are temporarily unavailable.",
                "description": (
                    "Keep the Barchart browser bridge running or configure "
                    "the official Barchart API credential."
                ),
                "source": "Barchart",
                "temporary": True,
                "retryable": True,
                "lastBridgeUpdate": None if state is None else state.get("capturedAt"),
                "bridgeAgeSeconds": None if age is None else round(age),
                "errors": errors,
            },
            headers={"Retry-After": "300"},
        )

    root_order = {root: index for index, root in enumerate(root_list)}
    rows.sort(key=lambda item: root_order.get(item["Root"], len(root_order)))

    return {
        "timestamp": iso_utc(now_utc()),
        "roots": root_list,
        "rows": len(rows),
        "partial": bool(errors),
        "errors": errors,
        "data": rows,
        "source": source_used,
    }


load_bridge_file()

app = CORSMiddleware(
    app=api,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS", "POST"],
    allow_headers=["*"],
)
