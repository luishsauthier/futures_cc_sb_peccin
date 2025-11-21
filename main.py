import requests
from urllib.parse import unquote
import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime

app = FastAPI()

# CORS liberado para qualquer origem
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_futures(root: str):
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
        "Open_Int": "Open_Int",
        "tradeTime": "Time",
    })

    df["Root"] = root
    cols = ["Root","Contract","Last","Change","Open","High","Low","Previous","Volume","Open_Int","Time"]
    return df[cols]

@app.get("/futures")
def read_futures(roots: str = "CC,SB"):
    root_list = [r.strip().upper() for r in roots.split(",") if r.strip()]
    dfs = [get_futures(root) for root in root_list]
    df_final = pd.concat(dfs, ignore_index=True)

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "data": df_final.to_dict(orient="records")
    }
