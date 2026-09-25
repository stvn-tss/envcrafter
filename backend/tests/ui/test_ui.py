import os

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page, expect

from tests.ui.conftest import LiveServer

pytestmark = pytest.mark.skipif(
    not os.environ.get("ENVCRAFTER_UI"), reason="set ENVCRAFTER_UI=1 to run browser tests"
)


def open_home(page: Page, server: LiveServer) -> list[str]:
    errors: list[str] = []
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(server.url)
    expect(page.locator(".template-card").first).to_be_visible()
    return errors


def deploy_template(page: Page, name: str, project: str) -> None:
    page.get_by_role("button", name=f"Details of {name}").click()
    page.locator("#project-name").fill(project)
    page.locator("#dialog-deploy").click()


def test_home_loads_cleanly_and_passes_axe(page: Page, live_server: LiveServer) -> None:
    errors = open_home(page, live_server)
    expect(page.locator("#engine-banner")).to_be_visible()
    expect(page.locator("#prompt-input")).to_be_disabled()
    expect(page.locator("#prompt-unavailable")).to_contain_text("ENVCRAFTER_LLM_API_KEY")

    results = Axe().run(page)
    serious = [
        v["id"] for v in results.response["violations"] if v["impact"] in {"serious", "critical"}
    ]
    assert serious == []
    assert errors == []


def test_deploy_then_stop_start_and_read_logs(page: Page, live_server: LiveServer) -> None:
    errors = open_home(page, live_server)
    deploy_template(page, "OWASP Juice Shop", "shop")
    expect(page.locator("#status-badge")).to_have_text("Ready", timeout=20_000)
    expect(page.locator("#job-result")).to_contain_text("http://shop.localhost")

    row = page.locator(".environment", has=page.locator("code", has_text="shop"))
    badge = row.locator(".status-badge")
    expect(badge).to_have_text("Running", timeout=15_000)
    row.get_by_role("button", name="Stop shop").click()
    expect(badge).to_have_text("Stopped", timeout=15_000)
    row.get_by_role("button", name="Start shop").click()
    expect(badge).to_have_text("Running", timeout=15_000)

    row.get_by_role("button", name="Logs of shop").click()
    expect(page.locator("#logs-list li").first).to_contain_text(
        "[simulated] juice-shop", timeout=10_000
    )
    page.locator("#logs-dialog [data-close]").click()
    assert errors == []


def test_reload_mid_deployment_reattaches(page: Page, slow_live_server: LiveServer) -> None:
    open_home(page, slow_live_server)
    deploy_template(page, "GLPI", "desk")
    expect(page.locator("#status-badge")).to_have_text("Deploying")

    page.reload()

    expect(page.locator("#job-title")).to_contain_text("desk", timeout=10_000)
    expect(page.locator("#status-badge")).to_have_text("Ready", timeout=30_000)


def test_plan_review_then_deploy(page: Page, live_server_with_llm: LiveServer) -> None:
    open_home(page, live_server_with_llm)
    page.locator("#prompt-input").fill("A vulnerable web app to practise the OWASP Top 10")
    page.get_by_role("button", name="Generate plan").click()

    dialog = page.locator("#plan-dialog")
    expect(dialog).to_be_visible(timeout=15_000)
    # For a "template" decision the review shows the catalog template's real name
    # (here "OWASP Juice Shop"), not the LLM's own title: the title must match what
    # actually gets deployed (see Orchestrator._plan_view). The FakeTranslator's summary
    # is untouched, so it still proves the translator's output drove this plan.
    expect(dialog).to_contain_text("OWASP Juice Shop")
    expect(dialog).to_contain_text("A deliberately vulnerable shop to practise the OWASP Top 10.")
    expect(dialog).to_contain_text("Intentionally vulnerable")
    dialog.locator("#plan-project-name").fill("lab")
    dialog.get_by_role("button", name="Deploy").click()

    expect(page.locator("#status-badge")).to_have_text("Ready", timeout=20_000)
    expect(page.locator("#job-result")).to_contain_text("http://lab.localhost")
