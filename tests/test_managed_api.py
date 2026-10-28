import asyncio
import json
from pathlib import Path

import azure.functions as func

from api.http_app import main


def test_managed_api_keeps_required_prefix_and_anonymous_http_binding():
    root = Path(__file__).resolve().parents[1] / "api"
    host = json.loads((root / "host.json").read_text())
    definition = json.loads((root / "http_app" / "function.json").read_text())
    assert host["extensions"]["http"]["routePrefix"] == "api"
    trigger = definition["bindings"][0]
    assert trigger["authLevel"] == "anonymous"
    assert trigger["route"] == "{*route}"
    assert trigger["methods"] == ["get", "post"]


def test_functions_adapter_preserves_api_path():
    request = func.HttpRequest(
        method="GET", url="https://demo.example/api/health", body=b"",
    )
    response = asyncio.run(main(request, None))
    assert response.status_code == 200
    assert json.loads(response.get_body())["status"] == "ok"
