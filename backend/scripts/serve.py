"""Local backend entry point, including psycopg's Windows event-loop requirement."""

import asyncio
import sys

import uvicorn


def main():
    server = uvicorn.Server(
        uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, loop="asyncio")
    )
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(server.serve(), loop_factory=factory)


if __name__ == "__main__":
    main()
