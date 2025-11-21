from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "hello from render"}

@app.get("/ping")
def ping():
    return {"ping": "pong"}
