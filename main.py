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


YAHOO_SYMBOLS = {
    "CC": "CC=F",
    "SB": "SB=F",
}


def number_or_none(value):
    if value is None or pd.isna(value):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer_or_none(value):
    if value is None or pd.isna(value):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_futures(root: str) -> pd.DataFrame:
    root = root.strip().upper()

    yahoo_symbol = YAHOO_SYMBOLS.get(root)

    if not yahoo_symbol:
        raise RuntimeError(
            f"O root {root} não está configurado no Yahoo Finance"
        )

    history = None
    last_error = None

    # Faz até três tentativas porque o Yahoo pode limitar
    # temporariamente requisições vindas de servidores em nuvem.
    for attempt in range(3):
        try:
            ticker = yf.Ticker(yahoo_symbol)

            history = ticker.history(
                period="1mo",
                interval="1d",
                auto_adjust=False,
                actions=False,
            )

            if history is not None and not history.empty:
                break

            last_error = RuntimeError(
                f"Yahoo Finance retornou histórico vazio para {root}"
            )

        except Exception as exc:
            last_error = exc
            logger.warning(
                "Tentativa %s falhou para root=%s: %s",
                attempt + 1,
                root,
                exc,
            )

        if attempt < 2:
            time.sleep(2 ** attempt)

    if history is None or history.empty:
        raise RuntimeError(
            f"Falha ao consultar Yahoo Finance para {root}: "
            f"{last_error}"
        )

    if "Close" not in history.columns:
        raise RuntimeError(
            f"Yahoo Finance não retornou a coluna Close para {root}"
        )

    history = history.dropna(subset=["Close"])

    if history.empty:
        raise RuntimeError(
            f"Yahoo Finance não retornou preços válidos para {root}"
        )

    current_row = history.iloc[-1]
    current_index = history.index[-1]

    previous_close = None

    if len(history) >= 2:
        previous_close = number_or_none(
            history.iloc[-2].get("Close")
        )

    last_price = number_or_none(current_row.get("Close"))

    change = None

    if last_price is not None and previous_close is not None:
        change = last_price - previous_close

    try:
        trade_time = current_index.isoformat()
    except AttributeError:
        trade_time = str(current_index)

    row = {
        "Root": root,
        "Contract": yahoo_symbol,
        "Last": last_price,
        "Change": change,
        "Open": number_or_none(current_row.get("Open")),
        "High": number_or_none(current_row.get("High")),
        "Low": number_or_none(current_row.get("Low")),
        "Previous": previous_close,
        "Volume": integer_or_none(current_row.get("Volume")),

        # Yahoo Finance não fornece esse campo
        # nesse histórico de contrato contínuo.
        "Open_Int": None,

        "Time": trade_time,
    }

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

    return pd.DataFrame(
        [row],
        columns=output_columns,
    )


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
                "source": "Yahoo Finance",
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
