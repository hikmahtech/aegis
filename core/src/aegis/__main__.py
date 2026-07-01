"""AEGIS v2 Core API entrypoint."""

import uvicorn

from aegis.telemetry import setup_telemetry

setup_telemetry()

from aegis.api.app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    uvicorn.run("aegis.__main__:app", host="0.0.0.0", port=8080, reload=True)
