"""Frontend HTTP & API Server for Semantic Autoscaling Dashboard.

Serves the modern web UI and provides JSON API endpoints:
- GET /api/status          -> AWS identity, stack status, and live connection info
- GET /api/trace           -> Pre-computed trace & forecast data for minute-by-minute playback
- GET /api/live-summary    -> Benchmark results from data/live/summary.json
- POST /api/classify       -> Live request forwarding to deployed API Gateway /classify
- POST /api/run-demo       -> Triggers the live AWS demo and streams/returns the output
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent

API_URL = "https://5l7bjmhfti.execute-api.ap-south-1.amazonaws.com"
REGION = "ap-south-1"
PROFILE = "semauto"
STACK_NAME = "semauto-live"

logger = logging.getLogger("semauto.server")


def load_trace_data() -> dict[str, Any]:
    """Load minute-by-minute metrics and trace forecasts for visualization."""
    eval_csv = ROOT_DIR / "data" / "evaluation_results.csv"
    labeled_csv = ROOT_DIR / "data" / "trace_labeled.csv"
    if not eval_csv.exists():
        return {"minutes": [], "series": {}}

    import pandas as pd
    eval_df = pd.read_csv(eval_csv)
    minutes = eval_df["minute"].tolist()

    # Load actual counts per archetype
    actual_agentic: list[float] = []
    actual_aggregate: list[float] = []
    forecast_agentic: list[float] = []
    forecast_aggregate: list[float] = []

    if labeled_csv.exists():
        labeled_df = pd.read_csv(labeled_csv)
        counts = labeled_df.groupby(["minute", "predicted_archetype"]).size().unstack(fill_value=0)
        agg_actual = counts.sum(axis=1)
        ag_actual = counts.get("agentic_tool_using", pd.Series(0, index=counts.index))

        # Re-index to match evaluation minutes
        agg_actual_sub = agg_actual.reindex(minutes, fill_value=50.0)
        ag_actual_sub = ag_actual.reindex(minutes, fill_value=5.0)

        actual_aggregate = [float(x) for x in agg_actual_sub]
        actual_agentic = [float(x) for x in ag_actual_sub]

        # Forecasts via exponential smoothing
        f_ag = ag_actual.ewm(alpha=0.15).mean().reindex(minutes, fill_value=5.0)
        f_agg = agg_actual.ewm(alpha=0.15).mean().reindex(minutes, fill_value=50.0)
        forecast_agentic = [round(float(x), 2) for x in f_ag]
        forecast_aggregate = [round(float(x), 2) for x in f_agg]

    def col_list(df: pd.DataFrame, col: str, default_val: float = 0.0) -> list[Any]:
        if col in df.columns:
            return df[col].tolist()
        return [default_val] * len(df)

    return {
        "minutes": minutes,
        "shift_start": 60,
        "shift_end": 75,
        "baseline_capacity": col_list(eval_df, "baseline_capacity", 2.0),
        "aware_capacity_total": col_list(eval_df, "aware_capacity_total", 4.0),
        "aware_capacity_agentic": col_list(eval_df, "aware_capacity_eks_gpu_reserved", 1.0),
        "aware_capacity_bedrock": col_list(eval_df, "aware_capacity_bedrock_on_demand", 1.0),
        "aware_capacity_sagemaker": col_list(eval_df, "aware_capacity_sagemaker_endpoint", 1.0),
        "aware_capacity_spot": col_list(eval_df, "aware_capacity_eks_gpu_spot", 1.0),
        "forecast_agentic": forecast_agentic,
        "forecast_aggregate": forecast_aggregate,
        "actual_agentic": actual_agentic,
        "actual_aggregate": actual_aggregate,
        "projected_spend_aware": col_list(eval_df, "aware_projected_spend", 22.0),
        "projected_spend_baseline": col_list(eval_df, "baseline_projected_spend", 22.0),
        "budget": 24.0,
    }


def load_live_summary() -> dict[str, Any]:
    summary_path = ROOT_DIR / "data" / "live" / "summary.json"
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except Exception as e:
            return {"error": str(e)}
    return {"runs": []}


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/status":
            self.handle_api_status()
        elif parsed.path == "/api/trace":
            self.handle_api_trace()
        elif parsed.path == "/api/live-summary":
            self.handle_api_live_summary()
        else:
            super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/classify":
            self.handle_api_classify()
        elif parsed.path == "/api/run-demo":
            self.handle_api_run_demo()
        else:
            self.send_error(404, "Endpoint not found")

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def handle_api_status(self) -> None:
        info: dict[str, Any] = {
            "api_url": API_URL,
            "region": REGION,
            "stack_name": STACK_NAME,
            "online": True,
            "cloudwatch_url": f"https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}#dashboards/dashboard/semauto-live",
        }
        try:
            import boto3
            session = boto3.Session(profile_name=PROFILE, region_name=REGION)
            sts = session.client("sts")
            ident = sts.get_caller_identity()
            info["account"] = ident["Account"]
            info["caller_arn"] = ident["Arn"]
            info["aws_connected"] = True
        except Exception as exc:
            info["aws_connected"] = False
            info["aws_error"] = str(exc)

        self._send_json(info)

    def handle_api_trace(self) -> None:
        try:
            data = load_trace_data()
            self._send_json(data)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def handle_api_live_summary(self) -> None:
        self._send_json(load_live_summary())

    def handle_api_classify(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8")
        try:
            payload = json.loads(body)
        except Exception:
            self._send_json({"error": "invalid json"}, status=400)
            return

        req = urllib.request.Request(
            f"{API_URL}/classify",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                elapsed = time.time() - t0
                res_data = json.loads(resp.read().decode("utf-8"))
                res_data["_elapsed_s"] = elapsed
                self._send_json(res_data, status=resp.status)
        except urllib.error.HTTPError as exc:
            self._send_json({"error": exc.read().decode("utf-8"), "status": exc.code}, status=exc.code)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def handle_api_run_demo(self) -> None:
        try:
            venv_python = ROOT_DIR / ".venv" / "bin" / "python"
            cmd = [str(venv_python if venv_python.exists() else sys.executable), "-m", "cloud.demo"]
            proc = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, timeout=60)
            self._send_json({
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)


def run_server(port: int = 8080) -> None:
    server_address = ("127.0.0.1", port)
    httpd = http.server.ThreadingHTTPServer(server_address, DashboardHandler)
    print(f"\n=======================================================")
    print(f"  SEMANTIC AUTOSCALING DASHBOARD RUNNING")
    print(f"  Local URL: http://localhost:{port}")
    print(f"=======================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_server(port)
