import logging
import re
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("futures-api")


# A aplicação FastAPI interna.
api = FastAPI(
    title="Futures API",
    version="2.0.0",
)


@api.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "API de futures está online",
        "endpoints": [
            "/ping",
            "/futures?roots=CC,SB",
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


def get_futures(root: str) -> pd.DataFrame:
    root = root.strip().upper()

    base_url = (
        f"https://www.barchart.com/"
        f"futures/quotes/{root}*0/futures-prices"
    )

    api_url = (
        "https://www.barchart.com/"
        "proxies/core-api/v1/quotes/get"
    )

    get_headers = {
        "accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/webp,*/*;q=0.8"
        ),
        "accept-language": "en-US,en;q=0.9",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    with requests.Session() as session:
        try:
            landing_response = session.get(
                base_url,
                headers=get_headers,
                timeout=(5, 20),
            )

            landing_response.raise_for_status()

        except requests.Timeout as exc:
            raise RuntimeError(
                f"Timeout ao acessar o Barchart para o root {root}"
            ) from exc

        except requests.RequestException as exc:
            raise RuntimeError(
                f"Falha HTTP ao acessar o Barchart para {root}: {exc}"
            ) from exc

        cookies = session.cookies.get_dict()
        token = cookies.get("XSRF-TOKEN")

        if not token:
            body_lower = landing_response.text.lower()

            anti_bot_detected = (
                "verify that you're not a robot" in body_lower
                or "enable javascript" in body_lower
                or "javascript is disabled" in body_lower
            )

            if anti_bot_detected:
                reason = "proteção anti-bot/JavaScript detectada"
            else:
                reason = "cookie XSRF-TOKEN não retornado"

            raise RuntimeError(
                f"Barchart indisponível para coleta: {reason}; "
                f"root={root}; HTTP={landing_response.status_code}"
            )

        xsrf_token = unquote(unquote(token))

        api_headers = {
            "accept": "application/json, text/plain, */*",
            "referer": base_url,
            "user-agent": get_headers["user-agent"],
            "x-xsrf-token": xsrf_token,
        }

        payload = {
            "fields": (
                "symbol,contractSymbol,lastPrice,priceChange,"
                "openPrice,highPrice,lowPrice,previousPrice,"
                "volume,openInterest,tradeTime"
            ),
            "list": "futures.contractInRoot",
            "root": root,
            "raw": "1",
        }

        try:
            quote_response = session.get(
                api_url,
                params=payload,
                headers=api_headers,
                timeout=(5, 20),
            )

            quote_response.raise_for_status()

        except requests.Timeout as exc:
            raise RuntimeError(
                f"Timeout ao consultar os contratos do root {root}"
            ) from exc

        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)

            raise RuntimeError(
                f"Falha na consulta do Barchart para {root}; "
                f"HTTP={status}; erro={exc}"
            ) from exc

        try:
            body = quote_response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Barchart retornou conteúdo não JSON para {root}"
            ) from exc

    data = body.get("data", [])

    if not data:
        raise RuntimeError(
            f"Nenhum contrato foi retornado para o root {root}"
        )

    required_columns = [
        "contractSymbol",
        "lastPrice",
        "priceChange",
        "openPrice",
        "highPrice",
        "lowPrice",
        "previousPrice",
        "volume",
        "openInterest",
        "tradeTime",
    ]

    df = pd.DataFrame(data)

    missing_columns = [
        column
        for column in required_columns
        if column not in df.columns
    ]

    if missing_columns:
        raise RuntimeError(
            "O formato retornado pelo Barchart mudou. "
            f"Colunas ausentes: {missing_columns}"
        )

    df = df[required_columns].rename(
        columns={
            "contractSymbol": "Contract",
            "lastPrice": "Last",
            "priceChange": "Change",
            "openPrice": "Open",
            "highPrice": "High",
            "lowPrice": "Low",
            "previousPrice": "Previous",
            "volume": "Volume",
            "openInterest": "Open_Int",
            "tradeTime": "Time",
        }
    )

    df["Root"] = root

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

    return df[output_columns]


@api.get("/futures")
def read_futures(roots: str = "CC,SB"):
    root_list = [
        root.strip().upper()
        for root in roots.split(",")
        if root.strip()
    ]

    # Remove duplicados mantendo a ordem.
    root_list = list(dict.fromkeys(root_list))

    if not root_list:
        raise HTTPException(
            status_code=400,
            detail="Informe pelo menos um root, por exemplo: CC,SB",
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

    frames = []
    errors = []

    for root in root_list:
        try:
            frames.append(get_futures(root))

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
                "message": "Falha ao consultar a fonte de contratos futuros",
                "source": "Barchart",
                "errors": errors,
            },
        )

    df_final = pd.concat(frames, ignore_index=True)

    # Remove valores que não podem ser serializados em JSON.
    df_final = df_final.replace(
        [float("inf"), float("-inf")],
        pd.NA,
    )

    df_final = (
        df_final
        .astype(object)
        .where(pd.notna(df_final), None)
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "roots": root_list,
        "rows": len(df_final),
        "partial": bool(errors),
        "errors": errors,
        "data": df_final.to_dict(orient="records"),
    }


# O CORS envolve a aplicação inteira.
# Dessa forma, até respostas 500/502 recebem os cabeçalhos CORS.
app = CORSMiddleware(
    app=api,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)
