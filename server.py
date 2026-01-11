from main import app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        ws_max_size=16 * 1024 * 1024,
    )
