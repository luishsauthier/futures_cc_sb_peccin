from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from datetime import timezone, timedelta

import requests
from urllib.parse import unquote
import pandas as pd

app = FastAPI()

# CORS liberado pra qualquer origem (depois dá pra restringir)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
@app.head("/")
def read_root():
    return {
        "status": "ok",
        "message": "API de futures está online",
        "endpoints": ["/ping", "/futures?roots=CC,SB"]
    }

@app.get("/ping")
def ping():
    return {"ping": "pong"}

@app.head("/ping")
def ping():
    return Response(status_code=200)

def get_futures(root: str) -> pd.DataFrame:
    root = root.strip().upper()

    base_url = f"https://www.barchart.com/futures/quotes/{root}*0/futures-prices"
    api_url = "https://www.barchart.com/proxies/core-api/v1/quotes/get"

    session = requests.Session()

    get_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    # 1) Coleta cookies + XSRF
    session.get(base_url, headers=get_headers)
    cookies = session.cookies.get_dict()

    if "XSRF-TOKEN" not in cookies:
        raise RuntimeError("XSRF token não encontrado. O site pode ter mudado.")

    xsrf_token = unquote(unquote(cookies["XSRF-TOKEN"]))

    api_headers = {
        "accept": "application/json, text/plain, */*",
        "referer": base_url,
        "user-agent": get_headers["user-agent"],
        "x-xsrf-token": xsrf_token,
    }

    payload = {
        "fields": (
            "symbol,contractSymbol,lastPrice,priceChange,openPrice,highPrice,"
            "lowPrice,previousPrice,volume,openInterest,tradeTime"
        ),
        "list": "futures.contractInRoot",
        "root": root,
        "raw": "1",
    }

    r = session.get(api_url, params=payload, headers=api_headers)
    r.raise_for_status()

    data = r.json().get("data", [])
    if not data:
        raise RuntimeError(f"Sem dados retornados para root {root}")

    df = pd.DataFrame(data)

    df = df[
        [
            "contractSymbol","lastPrice","priceChange","openPrice","highPrice",
            "lowPrice","previousPrice","volume","openInterest","tradeTime"
        ]
    ].rename(columns={
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
    })

    df["Root"] = root
    cols = ["Root", "Contract", "Last", "Change", "Open", "High",
            "Low", "Previous", "Volume", "Open_Int", "Time"]
    return df[cols]



INVESTING_HTML = "https://br.investing.com/currencies/usd-brl"
INVESTING_API  = "https://api.investing.com/api/financialdata/650/real-time"
AWESOME_API    = "https://economia.awesomeapi.com.br/json/all"

HEADERS_INVESTING = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

def fetch_dolar_investing():
    session = requests.Session()

    session.get(INVESTING_HTML, headers=HEADERS_INVESTING, timeout=10)

    r = session.get(
        INVESTING_API,
        headers={
            **HEADERS_INVESTING,
            "accept": "application/json",
            "referer": INVESTING_HTML,
            "x-requested-with": "XMLHttpRequest",
            "domain-id": "30",
        },
        timeout=10
    )

    # se não for 200, falha controlada
    if r.status_code != 200:
        raise RuntimeError(f"Investing HTTP {r.status_code}")

    # tenta JSON
    try:
        data = r.json()
    except Exception:
        raise RuntimeError("Investing retornou resposta não-JSON")

    # valida estrutura
    if "last" not in data or "lastUpdateTimestamp" not in data:
        raise RuntimeError(f"Estrutura inesperada: {data}")

    price = float(data["last"].replace(",", "."))
    ts = datetime.fromtimestamp(
        int(data["lastUpdateTimestamp"]),
        tz=timezone.utc
    )

    return price, ts

def fetch_dolar_awesome():
    r = requests.get(AWESOME_API, timeout=10)
    r.raise_for_status()

    usd = r.json()["USD"]
    price = float(usd["bid"])

    ts = datetime.fromisoformat(
        usd["create_date"].replace(" ", "T")
    ).replace(tzinfo=timezone.utc)

    return price, ts


@app.get("/futures")
def read_futures(roots: str = "CC,SB"):
    """
    Exemplo: /futures?roots=CC,SB,KC
    """
    root_list = [r.strip().upper() for r in roots.split(",") if r.strip()]

    if not root_list:
        return {"error": "Informe pelo menos um root, ex: ?roots=CC,SB"}

    dfs = [get_futures(root) for root in root_list]
    df_final = pd.concat(dfs, ignore_index=True)

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "roots": root_list,
        "rows": len(df_final),
        "data": df_final.to_dict(orient="records"),
    }

@app.get("/dolar")
def read_dolar():
    try:
        price, ts = fetch_dolar_investing()

        # se estiver atualizado (≤ 10 minutos)
        if datetime.now(timezone.utc) - ts <= timedelta(minutes=10):
            return {
                "source": "investing",
                "price": price,
                "datetime": ts.isoformat()
            }

    except Exception as e:
        # log simples (Render mostra no console)
        print("Investing falhou:", e)

    # fallback
    price, ts = fetch_dolar_awesome()
    return {
        "source": "awesomeapi",
        "price": price,
        "datetime": ts.isoformat()
    }


