import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("futures-api")


DEFAULT_BARCHART_URL = (
    "https://ondemand.websol.barchart.com/getQuote.json"
)
BARCHART_URL = (
    os.getenv("BARCHART_API_URL", DEFAULT_BARCHART_URL).strip()
    or DEFAULT_BARCHART_URL
)

REQUEST_TIMEOUT = (5, 30)
MAX_ATTEMPTS = 3
MAX_ROOTS_PER_REQUEST = 10
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = int(raw_value)
    except ValueError:
        logger.warning(
            "Valor inválido para %s=%r; usando %s",
            name,
            raw_value,
            default,
        )
        return default

    return max(value, minimum)


def env_list(name: str, default: str) -> tuple[str, ...]:
    raw_value = os.getenv(name, default)
    values = [
        item.strip().upper()
        for item in raw_value.split(",")
        if item.strip()
    ]

    # Remove duplicados preservando a ordem.
    return tuple(dict.fromkeys(values))


# Usa o novo nome, mas preserva compatibilidade com a variável antiga.
LEGACY_CACHE_TTL_SECONDS = env_int("CACHE_TTL_SECONDS", 60)
CACHE_TTL_SECONDS = env_int(
    "BARCHART_CACHE_TTL_SECONDS",
    LEGACY_CACHE_TTL_SECONDS,
)
SUPPORTED_ROOTS = env_list("SUPPORTED_ROOTS", "CC,SB")


# Cache simples em memória.
# Se houver mais de uma instância no Render, cada instância terá seu cache.
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_cache_lock = threading.Lock()


api = FastAPI(
    title="Futures API",
    version="3.1.0",
)


def get_api_key() -> str:
    return os.getenv("BARCHART_API_KEY", "").strip()


def source_unavailable_exception() -> HTTPException:
    """
    Resposta temporária e amigável enquanto a chave oficial não foi liberada.

    Mantemos HTTP 503 para o frontend diferenciar indisponibilidade da fonte
    de uma resposta válida sem cotações.
    """
    return HTTPException(
        status_code=503,
        detail={
            "code": "FUTURES_SOURCE_UNAVAILABLE",
            "message": (
                "Atualização de preços temporariamente indisponível."
            ),
            "description": (
                "Estamos aguardando a liberação de acesso à fonte oficial "
                "Barchart. Nenhum valor alternativo será exibido para "
                "evitar divergências."
            ),
            "source": "Barchart OnDemand",
            "temporary": True,
            "retryable": False,
        },
        headers={
            "Retry-After": "3600",
        },
    )


@api.get("/")
def read_root():
    api_key_configured = bool(get_api_key())

    return {
        "status": "ok",
        "message": "API de futures está online",
        "source": "Barchart OnDemand",
        "sourceStatus": (
            "ready"
            if api_key_configured
            else "awaiting_credentials"
        ),
        "apiKeyConfigured": api_key_configured,
        "cacheTtlSeconds": CACHE_TTL_SECONDS,
        "supportedRoots": list(SUPPORTED_ROOTS),
        "endpoints": [
            "/ping",
            "/futures?roots=CC,SB",
            "/futures?roots=CC&refresh=true",
        ],
    }


@api.head("/")
def read_root_head():
    return Response(status_code=200)


@api.get("/ping")
def ping():
    return {"ping": "pong"}


@api.head("/ping")
def ping_head():
    return Response(status_code=200)


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer_or_none(value: Any) -> int | None:
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def first_not_none(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue

        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            continue

        return value

    return None


def calculate_previous_price(
    last_price: Any,
    net_change: Any,
    explicit_previous: Any = None,
) -> float | None:
    previous = number_or_none(explicit_previous)

    if previous is not None:
        return previous

    last = number_or_none(last_price)
    change = number_or_none(net_change)

    if last is None or change is None:
        return None

    return last - change


def get_cached_futures(root: str) -> pd.DataFrame | None:
    if CACHE_TTL_SECONDS <= 0:
        return None

    now = time.monotonic()

    with _cache_lock:
        cached = _cache.get(root)

        if cached is None:
            return None

        created_at, dataframe = cached

        if now - created_at >= CACHE_TTL_SECONDS:
            _cache.pop(root, None)
            return None

        return dataframe.copy(deep=True)


def set_cached_futures(
    root: str,
    dataframe: pd.DataFrame,
) -> None:
    if CACHE_TTL_SECONDS <= 0:
        return

    with _cache_lock:
        _cache[root] = (
            time.monotonic(),
            dataframe.copy(deep=True),
        )


def request_barchart(root: str) -> dict[str, Any]:
    api_key = get_api_key()

    if not api_key:
        # Proteção adicional para chamadas internas diretas.
        # A rota /futures trata este caso antes de iniciar as consultas.
        raise RuntimeError(
            "Fonte Barchart ainda não configurada"
        )

    payload = {
        "apikey": api_key,
        # ^F solicita todos os contratos futuros do root.
        # Exemplos: CC^F e SB^F.
        "symbols": f"{root}^F",
        # Campos adicionais de interesse.
        "fields": "openInterest,previousClose",
    }

    response: requests.Response | None = None
    last_error: Exception | None = None
    started_at = time.monotonic()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                BARCHART_URL,
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": (
                        "application/x-www-form-urlencoded"
                    ),
                    "User-Agent": "futures-cc-sb-peccin/3.1",
                },
                timeout=REQUEST_TIMEOUT,
            )

            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt < MAX_ATTEMPTS
            ):
                logger.warning(
                    "Barchart retornou HTTP %s para root=%s; "
                    "tentativa %s/%s",
                    response.status_code,
                    root,
                    attempt,
                    MAX_ATTEMPTS,
                )
                time.sleep(2 ** (attempt - 1))
                continue

            break

        except requests.Timeout as exc:
            last_error = exc

            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "Timeout no Barchart para root=%s; "
                    "tentativa %s/%s",
                    root,
                    attempt,
                    MAX_ATTEMPTS,
                )
                time.sleep(2 ** (attempt - 1))
                continue

            raise RuntimeError(
                f"Timeout ao consultar o Barchart para o root {root}"
            ) from exc

        except requests.RequestException as exc:
            last_error = exc

            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "Falha de rede no Barchart para root=%s; "
                    "tentativa %s/%s: %s",
                    root,
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                )
                time.sleep(2 ** (attempt - 1))
                continue

            raise RuntimeError(
                "Falha de rede ao consultar o Barchart para "
                f"{root}: {exc}"
            ) from exc

    if response is None:
        raise RuntimeError(
            "Não foi possível obter resposta do Barchart para "
            f"{root}: {last_error}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        logger.error(
            "Barchart retornou resposta não JSON; root=%s; HTTP=%s",
            root,
            response.status_code,
        )
        raise RuntimeError(
            "Barchart retornou conteúdo que não é JSON; "
            f"root={root}; HTTP={response.status_code}"
        ) from exc

    if not isinstance(body, dict):
        raise RuntimeError(
            "Barchart retornou um formato de resposta inesperado; "
            f"root={root}; HTTP={response.status_code}"
        )

    api_status = body.get("status") or {}

    if not isinstance(api_status, dict):
        api_status = {}

    api_code = api_status.get("code")
    api_message = api_status.get(
        "message",
        "Mensagem não informada",
    )
    results = body.get("results") or []

    elapsed_ms = round(
        (time.monotonic() - started_at) * 1000
    )

    logger.info(
        "Consulta Barchart concluída; root=%s; HTTP=%s; "
        "apiCode=%s; contratos=%s; elapsedMs=%s",
        root,
        response.status_code,
        api_code,
        len(results) if isinstance(results, list) else 0,
        elapsed_ms,
    )

    http_success = 200 <= response.status_code < 300
    api_success = str(api_code) == "200"

    if not http_success or not api_success:
        raise RuntimeError(
            "Barchart recusou a consulta; "
            f"root={root}; "
            f"HTTP={response.status_code}; "
            f"código={api_code}; "
            f"mensagem={api_message}"
        )

    if not isinstance(results, list):
        raise RuntimeError(
            "Barchart retornou results em formato inesperado; "
            f"root={root}"
        )

    return body


def get_futures(
    root: str,
    force_refresh: bool = False,
) -> pd.DataFrame:
    root = root.strip().upper()

    if not force_refresh:
        cached = get_cached_futures(root)

        if cached is not None:
            logger.info(
                "Cache utilizado para root=%s",
                root,
            )
            return cached

    body = request_barchart(root)
    results = body.get("results") or []

    if not results:
        raise RuntimeError(
            "Nenhum contrato futuro foi retornado para "
            f"o root {root}"
        )

    rows: list[dict[str, Any]] = []

    for item in results:
        if not isinstance(item, dict):
            logger.warning(
                "Registro ignorado por formato inválido; root=%s",
                root,
            )
            continue

        contract = item.get("symbol")

        if not contract:
            logger.warning(
                "Registro ignorado porque não possui symbol; root=%s",
                root,
            )
            continue

        last_price = item.get("lastPrice")
        net_change = item.get("netChange")

        open_interest = first_not_none(
            item.get("openInterest"),
            item.get("previousOpenInterest"),
        )

        rows.append(
            {
                "Root": root,
                "Contract": contract,
                "Last": number_or_none(last_price),
                "Change": number_or_none(net_change),
                "Open": number_or_none(item.get("open")),
                "High": number_or_none(item.get("high")),
                "Low": number_or_none(item.get("low")),
                "Previous": calculate_previous_price(
                    last_price=last_price,
                    net_change=net_change,
                    explicit_previous=item.get(
                        "previousClose"
                    ),
                ),
                "Volume": integer_or_none(
                    item.get("volume")
                ),
                "Open_Int": integer_or_none(
                    open_interest
                ),
                "Time": first_not_none(
                    item.get("tradeTimestamp"),
                    item.get("serverTimestamp"),
                ),
            }
        )

    if not rows:
        raise RuntimeError(
            "O Barchart respondeu, mas não retornou "
            f"contratos válidos para {root}"
        )

    output_columns = [
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

    dataframe = pd.DataFrame(
        rows,
        columns=output_columns,
    )

    set_cached_futures(
        root,
        dataframe,
    )

    return dataframe.copy(deep=True)


def normalize_dataframe(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    normalized = dataframe.replace(
        [float("inf"), float("-inf")],
        pd.NA,
    )

    return (
        normalized
        .astype(object)
        .where(pd.notna(normalized), None)
    )


@api.get("/futures")
def read_futures(
    roots: str = "CC,SB",
    refresh: bool = False,
):
    root_list = [
        root.strip().upper()
        for root in roots.split(",")
        if root.strip()
    ]

    # Remove duplicados mantendo a ordem original.
    root_list = list(dict.fromkeys(root_list))

    if not root_list:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_ROOTS",
                "message": (
                    "Informe pelo menos um root, "
                    "por exemplo: CC,SB"
                ),
            },
        )

    if len(root_list) > MAX_ROOTS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ROOT_LIMIT_EXCEEDED",
                "message": (
                    "O limite e de "
                    f"{MAX_ROOTS_PER_REQUEST} roots por requisição"
                ),
            },
        )

    invalid_roots = [
        root
        for root in root_list
        if not re.fullmatch(r"[A-Z0-9]{1,8}", root)
    ]

    if invalid_roots:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_ROOTS",
                "message": "Foram informados roots inválidos",
                "roots": invalid_roots,
            },
        )

    unsupported_roots = [
        root
        for root in root_list
        if root not in SUPPORTED_ROOTS
    ]

    if unsupported_roots:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "UNSUPPORTED_ROOTS",
                "message": "Foram informados roots não suportados",
                "roots": unsupported_roots,
                "supportedRoots": list(SUPPORTED_ROOTS),
            },
        )

    # Esta verificação precisa ocorrer antes do loop.
    # Assim o 503 amigável não é capturado e transformado em 502.
    if not get_api_key():
        logger.info(
            "Consulta de futures bloqueada temporariamente: "
            "BARCHART_API_KEY ainda não configurada; roots=%s",
            ",".join(root_list),
        )
        raise source_unavailable_exception()

    logger.info(
        "Consulta de futures iniciada; roots=%s; refresh=%s",
        ",".join(root_list),
        refresh,
    )

    frames: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []

    for root in root_list:
        try:
            frames.append(
                get_futures(
                    root=root,
                    force_refresh=refresh,
                )
            )

        except Exception as exc:
            logger.exception(
                "Erro ao consultar contratos para root=%s",
                root,
            )

            errors.append(
                {
                    "root": root,
                    "error": str(exc),
                }
            )

    if not frames:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "FUTURES_PROVIDER_ERROR",
                "message": (
                    "Falha ao consultar a fonte "
                    "de contratos futuros"
                ),
                "source": "Barchart OnDemand",
                "errors": errors,
            },
        )

    df_final = pd.concat(
        frames,
        ignore_index=True,
    )
    df_final = normalize_dataframe(df_final)

    return {
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "roots": root_list,
        "rows": len(df_final),
        "partial": bool(errors),
        "errors": errors,
        "data": df_final.to_dict(
            orient="records"
        ),
    }


# O CORS envolve toda a aplicação,
# inclusive as respostas de erro.
app = CORSMiddleware(
    app=api,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=[
        "GET",
        "HEAD",
        "OPTIONS",
    ],
    allow_headers=["*"],
)
