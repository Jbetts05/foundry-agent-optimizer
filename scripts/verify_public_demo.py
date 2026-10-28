"""Measure the deployed comparison through its actual browser UI, with full response evidence."""

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def deployment_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError(
            "Provide an HTTP(S) deployment origin without credentials, path, query, or fragment."
        )
    return value.rstrip("/")


def verify(destination: Path, url: str):
    if destination.exists():
        raise FileExistsError("Choose a new evidence path; previous measurements are immutable.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "url": url, "created_at": datetime.now(UTC).isoformat(),
        "execution": "public-browser-http", "responses": [], "browser_errors": [],
        "holdout_used_for_tuning": False,
    }

    def save():
        destination.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    save()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: state["browser_errors"].append(str(error)))

        def response_received(response):
            if response.url.endswith("/api/run"):
                state["responses"].append({
                    "request": response.request.post_data_json,
                    "http_status": response.status, "response": response.json(),
                })
                save()
                latest = state["responses"][-1]
                print(json.dumps({
                    "completed": len(state["responses"]), **latest["request"],
                    "http_status": response.status,
                    "all_checks_passed": all(
                        check["passed"] for check in latest["response"].get("checks", [])
                    ) if latest["response"].get("checks") else False,
                }), flush=True)

        page.on("response", response_received)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_function(
            "document.getElementById('v2-version').textContent === 'Version 2'",
            timeout=60000,
        )
        state["catalog"] = page.request.get(url + "/api/catalog").json()
        if state["catalog"]["agent_name"] != "promotion-policy-experiment":
            raise ValueError("The public website is not bound to the experiment.")
        if state["catalog"]["v1"] != "1" or state["catalog"]["v2"]["version"] != "2":
            raise ValueError("The public comparison is not pinned to the approved versions.")
        page.locator("#evaluate").click()
        page.wait_for_function(
            "() => { const text = document.getElementById('evaluation-status').textContent; "
            "return text === 'Complete' || text.startsWith('Incomplete:'); }",
            timeout=1200000,
        )
        state["evaluation_status"] = page.locator("#evaluation-status").inner_text()
        state["evaluation_table"] = page.locator("#evaluation").inner_text()
        cases = {case["id"]: case for case in state["catalog"]["cases"]}
        summary = defaultdict(lambda: {
            "trials": 0, "fully_correct": 0, "decisions_correct": 0, "request_errors": 0,
        })
        for row in state["responses"]:
            case = cases[row["request"]["case_id"]]
            group = summary[(case["split"], row["request"]["version"])]
            group["trials"] += 1
            payload = row["response"]
            if row["http_status"] != 200 or payload.get("status") != "completed":
                group["request_errors"] += 1
                continue
            group["decisions_correct"] += payload["result"]["decision"] == case["expected"]
            group["fully_correct"] += bool(payload["checks"]) and all(
                check["passed"] for check in payload["checks"]
            )
        state["summary"] = [
            {"split": split, "version": version, **values}
            for (split, version), values in sorted(summary.items())
        ]
        state["completed_at"] = datetime.now(UTC).isoformat()
        page.locator("#evaluation").screenshot(
            path=str(destination.with_name(destination.stem + "-measurements.png")),
        )
        page.screenshot(path=str(destination.with_suffix(".png")), full_page=True)
        save()
        browser.close()
    if len(state["responses"]) != 40 or state["browser_errors"]:
        raise RuntimeError("The browser did not complete forty responses without script errors.")
    print(json.dumps({"summary": state["summary"], "status": state["evaluation_status"]}), flush=True)
    if state["evaluation_status"] != "Complete":
        raise RuntimeError("Some public requests failed; evidence includes every failure.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", type=deployment_url, required=True,
        help="Origin of your own deployed comparison website.",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "agent" / ".foundry" / "results" / "public-reviewed-comparison.json",
    )
    arguments = parser.parse_args()
    verify(arguments.output, arguments.url)
