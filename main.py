import hmac
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd
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

LEGACY_BARCHART_PAGE_URL = (
    "https://www.barchart.com/"
    "futures/quotes/{root}%2A0/futures-prices"
)

LEGACY_BARCHART_PROXY_URL = (
    "https://www.barchart.com/"
    "proxies/core-api/v1/quotes/get"
)

REQUEST_TIMEOUT = (5, 30)
LEGACY_REQUEST_TIMEOUT = (5, 20)
MAX_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


# -----------------------------------------------------------------------------
# Configuracao
# -----------------------------------------------------------------------------


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = int(raw_value)
    except ValueError:
        logger.warning(
            "Valor invalido para %s=%r; usando %s",
            name,
            raw_value,
            default,
        )
        return default

    return max(value, minimum)


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(
        name,
        "true" if default else "false",
    )

    return raw_value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def parse_supported_roots() -> tuple[str, ...]:
    raw_value = os.getenv("SUPPORTED_ROOTS", "CC,SB")

    roots = [
        root.strip().upper()
        for root in raw_value.split(",")
        if root.strip()
    ]

    valid_roots = [
        root
        for root in roots
        if re.fullmatch(r"[A-Z0-9]{1,8}", root)
    ]

    if not valid_roots:
        logger.warning(
            "SUPPORTED_ROOTS nao possui valores validos; usando CC,SB"
        )
        return ("CC", "SB")

    return tuple(dict.fromkeys(valid_roots))


# Mantem compatibilidade com a variavel antiga CACHE_TTL_SECONDS.
_legacy_cache_ttl = env_int("CACHE_TTL_SECONDS", 60)
CACHE_TTL_SECONDS = env_int(
    "BARCHART_CACHE_TTL_SECONDS",
    _legacy_cache_ttl,
)
SUPPORTED_ROOTS = parse_supported_roots()

# Cache simples em memoria. Cada instancia do Render possui seu proprio cache.
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_cache_lock = threading.Lock()


# -----------------------------------------------------------------------------
# Aplicacao e rotas basicas
# -----------------------------------------------------------------------------

api = FastAPI(
    title="Futures API",
    version="3.1.0",
)


@api.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "API de futures está online",
        "source": "Barchart OnDemand",
        "apiKeyConfigured": bool(get_api_key()),
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


# -----------------------------------------------------------------------------
# Funcoes compartilhadas
# -----------------------------------------------------------------------------


def get_api_key() -> str:
    return os.getenv("BARCHART_API_KEY", "").strip()


def source_unavailable_exception() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "FUTURES_SOURCE_UNAVAILABLE",
            "message": (
                "A atualização automática de preços está "
                "temporariamente indisponível."
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


def number_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer_or_none(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None and not pd.isna(value):
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


def normalize_dataframe_for_json(
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


# -----------------------------------------------------------------------------
# Cache da API oficial
# -----------------------------------------------------------------------------


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


# -----------------------------------------------------------------------------
# API oficial Barchart OnDemand
# -----------------------------------------------------------------------------


def request_barchart(root: str) -> dict[str, Any]:
    api_key = get_api_key()

    # A rota /futures ja faz a validacao amigavel. Esta verificacao protege
    # chamadas internas diretas sem expor o nome da variavel ao consumidor.
    if not api_key:
        raise RuntimeError("Credencial oficial do Barchart indisponível")

    payload = {
        "apikey": api_key,
        # ^F solicita todos os contratos futuros do root.
        "symbols": f"{root}^F",
        "fields": "openInterest,previousClose",
    }

    response: requests.Response | None = None
    last_error: Exception | None = None
    started_at = time.monotonic()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                BARCHART_API_URL,
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
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
                    "Timeout no Barchart para root=%s; tentativa %s/%s",
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
                f"Falha de rede ao consultar o Barchart para {root}: {exc}"
            ) from exc

    if response is None:
        raise RuntimeError(
            "Não foi possível obter resposta do Barchart para "
            f"{root}: {last_error}"
        )

    elapsed_ms = round((time.monotonic() - started_at) * 1000)

    if response.status_code == 204:
        raise RuntimeError(
            f"Barchart nao retornou conteudo para o root {root}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        preview = response.text[:200].replace("\n", " ")

        raise RuntimeError(
            "Barchart retornou conteúdo que não é JSON; "
            f"root={root}; HTTP={response.status_code}; "
            f"resposta={preview!r}"
        ) from exc

    api_status = body.get("status") or {}
    api_code = api_status.get("code")
    api_message = api_status.get(
        "message",
        "Mensagem nao informada",
    )

    logger.info(
        "Consulta oficial concluida root=%s http=%s apiCode=%s "
        "elapsedMs=%s",
        root,
        response.status_code,
        api_code,
        elapsed_ms,
    )

    if response.status_code >= 400 or str(api_code) != "200":
        raise RuntimeError(
            "Barchart recusou a consulta; "
            f"root={root}; "
            f"HTTP={response.status_code}; "
            f"código={api_code}; "
            f"mensagem={api_message}"
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
            logger.info("Cache utilizado para root=%s", root)
            return cached

    body = request_barchart(root)
    results = body.get("results") or []

    if not results:
        raise RuntimeError(
            f"Nenhum contrato futuro foi retornado para o root {root}"
        )

    rows: list[dict[str, Any]] = []

    for item in results:
        contract = item.get("symbol")

        if not contract:
            logger.warning(
                "Registro ignorado porque nao possui symbol; root=%s",
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
                    explicit_previous=item.get("previousClose"),
                ),
                "Volume": integer_or_none(item.get("volume")),
                "Open_Int": integer_or_none(open_interest),
                "Time": first_not_none(
                    item.get("tradeTimestamp"),
                    item.get("serverTimestamp"),
                ),
            }
        )

    if not rows:
        raise RuntimeError(
            "O Barchart respondeu, mas nao retornou "
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

    dataframe = pd.DataFrame(rows, columns=output_columns)
    set_cached_futures(root, dataframe)

    logger.info(
        "Contratos oficiais normalizados root=%s rows=%s",
        root,
        len(dataframe),
    )

    return dataframe.copy(deep=True)


# -----------------------------------------------------------------------------
# Diagnostico temporario do método antigo
# -----------------------------------------------------------------------------


def validate_diagnostic_access(
    received_token: str | None,
) -> None:
    enabled = env_bool("ENABLE_LEGACY_DIAGNOSTIC", False)
    expected_token = os.getenv(
        "LEGACY_DIAGNOSTIC_TOKEN",
        "",
    ).strip()

    # Responde 404 quando estiver desativado ou quando o token for invalido,
    # evitando expor a existencia do endpoint.
    if not enabled or not expected_token or not received_token:
        raise HTTPException(status_code=404)

    if not hmac.compare_digest(received_token, expected_token):
        raise HTTPException(status_code=404)


@api.get(
    "/diagnostics/legacy-barchart",
    include_in_schema=False,
)
def diagnose_legacy_barchart(
    root: str = "CC",
    x_diagnostic_token: str | None = Header(
        default=None,
        alias="X-Diagnostic-Token",
    ),
):
    """
    Testa, a partir da própria instância do Render, se o fluxo antigo voltou.

    O diagnostico não resolve CAPTCHA, não executa JavaScript, não contorna
    proteção anti-bot e nunca devolve o conteudo do XSRF-TOKEN.
    """

    validate_diagnostic_access(x_diagnostic_token)

    root = root.strip().upper()

    if root not in SUPPORTED_ROOTS:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Root não permitido no diagnostico",
                "supportedRoots": list(SUPPORTED_ROOTS),
            },
        )

    page_url = LEGACY_BARCHART_PAGE_URL.format(root=root)

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    page_headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": user_agent,
    }

    challenge_markers = [
        "verify that you're not a robot",
        "verify you are human",
        "enable javascript",
        "javascript is disabled",
        "checking your browser",
        "pardon our interruption",
        "captcha",
    ]

    started_at = time.monotonic()

    try:
        with requests.Session() as session:
            page_response = session.get(
                page_url,
                headers=page_headers,
                timeout=LEGACY_REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            page_elapsed_ms = round(
                (time.monotonic() - started_at) * 1000
            )

            cookies = session.cookies.get_dict()
            body_lower = page_response.text.lower()

            challenge_detected = any(
                marker in body_lower
                for marker in challenge_markers
            )

            xsrf_cookie = cookies.get("XSRF-TOKEN")
            xsrf_present = bool(xsrf_cookie)

            page_result = {
                "httpStatus": page_response.status_code,
                "contentType": page_response.headers.get("content-type"),
                "elapsedMs": page_elapsed_ms,
                "bodyLength": len(page_response.content),
                "cookieNames": sorted(cookies.keys()),
                "xsrfTokenPresent": xsrf_present,
                "challengeDetected": challenge_detected,
                "finalUrlHost": urlparse(page_response.url).hostname,
            }

            logger.info(
                "Diagnostico legacy pagina root=%s http=%s xsrf=%s "
                "challenge=%s elapsedMs=%s",
                root,
                page_response.status_code,
                xsrf_present,
                challenge_detected,
                page_elapsed_ms,
            )

            # O proxy só é testado quando o site forneceu naturalmente o
            # cookie e não há indício de desafio anti-bot.
            if not xsrf_present or challenge_detected:
                return {
                    "diagnostic": "legacy-barchart",
                    "executedFrom": "render-service",
                    "root": root,
                    "page": page_result,
                    "proxy": {
                        "attempted": False,
                        "reason": (
                            "XSRF-TOKEN ausente ou proteção anti-bot "
                            "detectada"
                        ),
                    },
                    "legacyUsable": False,
                }

            xsrf_token = unquote(unquote(xsrf_cookie))

            proxy_headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": page_url,
                "User-Agent": user_agent,
                "X-XSRF-TOKEN": xsrf_token,
            }

            proxy_params = {
                "fields": (
                    "symbol,contractSymbol,lastPrice,priceChange,"
                    "openPrice,highPrice,lowPrice,previousPrice,"
                    "volume,openInterest,tradeTime"
                ),
                "list": "futures.contractInRoot",
                "root": root,
                "raw": "1",
            }

            proxy_started_at = time.monotonic()

            proxy_response = session.get(
                LEGACY_BARCHART_PROXY_URL,
                params=proxy_params,
                headers=proxy_headers,
                timeout=LEGACY_REQUEST_TIMEOUT,
            )

            proxy_elapsed_ms = round(
                (time.monotonic() - proxy_started_at) * 1000
            )

            json_valid = False
            rows = 0
            response_keys: list[str] = []
            proxy_error: str | None = None

            try:
                proxy_body = proxy_response.json()
                json_valid = True

                if isinstance(proxy_body, dict):
                    response_keys = sorted(proxy_body.keys())
                    proxy_data = proxy_body.get("data")

                    if isinstance(proxy_data, list):
                        rows = len(proxy_data)

            except ValueError:
                proxy_error = "O proxy retornou conteúdo que não é JSON"

            legacy_usable = (
                proxy_response.status_code == 200
                and json_valid
                and rows > 0
            )

            logger.info(
                "Diagnostico legacy proxy root=%s http=%s json=%s "
                "rows=%s usable=%s elapsedMs=%s",
                root,
                proxy_response.status_code,
                json_valid,
                rows,
                legacy_usable,
                proxy_elapsed_ms,
            )

            return {
                "diagnostic": "legacy-barchart",
                "executedFrom": "render-service",
                "root": root,
                "page": page_result,
                "proxy": {
                    "attempted": True,
                    "httpStatus": proxy_response.status_code,
                    "contentType": proxy_response.headers.get(
                        "content-type"
                    ),
                    "elapsedMs": proxy_elapsed_ms,
                    "jsonValid": json_valid,
                    "responseKeys": response_keys,
                    "rows": rows,
                    "error": proxy_error,
                },
                "legacyUsable": legacy_usable,
            }

    except requests.Timeout:
        logger.warning(
            "Timeout no diagnostico legacy para root=%s",
            root,
        )

        return {
            "diagnostic": "legacy-barchart",
            "executedFrom": "render-service",
            "root": root,
            "legacyUsable": False,
            "networkError": "Timeout ao acessar o Barchart",
        }

    except requests.RequestException as exc:
        logger.warning(
            "Falha de rede no diagnostico legacy para root=%s: %s",
            root,
            exc,
        )

        return {
            "diagnostic": "legacy-barchart",
            "executedFrom": "render-service",
            "root": root,
            "legacyUsable": False,
            "networkError": "Falha de rede ao acessar o Barchart",
        }


# -----------------------------------------------------------------------------
# Endpoint publico de contratos futuros
# -----------------------------------------------------------------------------


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
            detail=(
                "Informe pelo menos um root, por exemplo: CC,SB"
            ),
        )

    if len(root_list) > 10:
        raise HTTPException(
            status_code=400,
            detail="O limite é de 10 roots por requisição",
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
                "message": "Root não suportado",
                "roots": unsupported_roots,
                "supportedRoots": list(SUPPORTED_ROOTS),
            },
        )

    if not get_api_key():
        raise source_unavailable_exception()

    logger.info(
        "Consulta /futures roots=%s refresh=%s",
        root_list,
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
                "message": (
                    "Falha ao consultar a fonte de contratos futuros"
                ),
                "source": "Barchart OnDemand",
                "errors": errors,
            },
        )

    df_final = pd.concat(frames, ignore_index=True)
    df_final = normalize_dataframe_for_json(df_final)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "roots": root_list,
        "rows": len(df_final),
        "partial": bool(errors),
        "errors": errors,
        "data": df_final.to_dict(orient="records"),
    }


# O CORS envolve toda a aplicacao, inclusive respostas de erro.
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
