"""Vercel API for the Semantic Autoscaling demo dashboard."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

ROOT_DIR = Path(__file__).resolve().parent.parent
AWS_API_URL = os.environ.get(
    "SEMAUTO_API_URL",
    "https://5l7bjmhfti.execute-api.ap-south-1.amazonaws.com",
)


def _json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _trace_data() -> dict[str, Any]:
    """Load the precomputed trace payload shipped with the frontend."""
    return _json_file(
        ROOT_DIR / "web" / "trace.json",
        {"minutes": [], "series": {}, "error": "trace data is unavailable"},
    )


@app.get("/api/status")
def status() -> dict[str, Any]:
    region = "ap-south-1"
    return {
        "api_url": AWS_API_URL,
        "region": region,
        "stack_name": "semauto-live",
        "online": True,
        "aws_connected": True,
        "cloudwatch_url": (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/semauto-live"
        ),
    }


@app.get("/api/trace")
def trace() -> dict[str, Any]:
    return _trace_data()


@app.get("/api/live-summary")
def live_summary() -> dict[str, Any]:
    return _json_file(ROOT_DIR / "web" / "live-summary.json", {"runs": []})


@app.post("/api/classify")
async def classify(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    started = time.monotonic()
    upstream = urllib.request.Request(
        f"{AWS_API_URL}/classify",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upstream, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            result["_elapsed_s"] = time.monotonic() - started
            return JSONResponse(result, status_code=response.status)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        return JSONResponse({"error": body, "status": error.code}, status_code=error.code)
    except (OSError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@app.post("/api/run-demo")
def run_demo() -> JSONResponse:
    return JSONResponse(
        {
            "error": (
                "The live demo runner is local-only. Use the dashboard controls "
                "or run `python -m cloud.demo` locally."
            )
        },
        status_code=501,
    )
