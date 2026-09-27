import os
from typing import Literal

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page, expect

from app.engine.base import HostResources
from app.engine.simulated import SimulatedEngine
from tests.conftest import FakeTranslator
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


def serious_violations(page: Page) -> list[str]:
    results = Axe().run(page)
    return [
        v["id"] for v in results.response["violations"] if v["impact"] in {"serious", "critical"}
    ]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_home_loads_cleanly_and_passes_axe(
    page: Page, live_server: LiveServer, color_scheme: Literal["light", "dark"]
) -> None:
    page.emulate_media(color_scheme=color_scheme)
    errors = open_home(page, live_server)
    expect(page.locator("#engine-banner")).to_be_visible()
    # Without a key the request form still works: it searches the catalog.
    expect(page.locator("#prompt-input")).to_be_enabled()
    expect(page.locator("#prompt-unavailable")).to_contain_text("Claude API key")
    expect(page.get_by_role("button", name="Find templates")).to_be_visible()
    expect(page.locator("#setup-check")).to_be_visible()

    assert serious_violations(page) == []
    page.get_by_role("button", name="Settings").click()
    expect(page.locator("#settings-dialog")).to_be_visible()
    expect(page.locator("#settings-system")).to_contain_text("Docker")
    assert serious_violations(page) == []
    assert errors == []


def test_without_a_key_the_request_finds_templates(page: Page, live_server: LiveServer) -> None:
    errors = open_home(page, live_server)
    page.locator("#prompt-input").fill("Supervision réseau avec alertes")

    match = page.locator("#prompt-matches").get_by_role("button", name="Open Zabbix")
    expect(match).to_be_visible()
    match.click()
    expect(page.locator("#template-dialog")).to_be_visible()
    expect(page.locator("#dialog-title")).to_have_text("Zabbix")
    page.locator("#dialog-cancel").click()

    page.locator("#prompt-input").fill("kubernetes cluster")
    page.get_by_role("button", name="Find templates").click()
    expect(page.locator("#prompt-no-match")).to_be_visible()
    assert errors == []


def test_api_key_saved_from_settings_turns_ai_plans_on(page: Page, live_server: LiveServer) -> None:
    errors = open_home(page, live_server)
    page.get_by_role("button", name="Add an API key").click()
    dialog = page.locator("#settings-dialog")
    expect(dialog.locator("#llm-status")).to_contain_text("No key")
    expect(dialog.locator("#llm-key")).to_be_focused()

    dialog.locator("#llm-key").fill("not a key")
    dialog.get_by_role("button", name="Save key").click()
    expect(dialog.locator("#llm-error")).to_contain_text("Paste the whole key")

    dialog.locator("#llm-key").fill("sk-ant-api03-rejected-by-the-fake-api")
    dialog.get_by_role("button", name="Save key").click()
    expect(dialog.locator("#llm-error")).to_contain_text("rejected this key")

    dialog.locator("#llm-key").fill("sk-ant-api03-accepted-by-the-fake-a1b2")
    dialog.get_by_role("button", name="Save key").click()
    expect(dialog.locator("#llm-status")).to_contain_text("key ending …a1b2")
    expect(dialog.locator("#llm-key")).to_have_value("")
    dialog.locator("[data-close]").click()
    expect(page.get_by_role("button", name="Generate plan")).to_be_visible()
    expect(page.locator("#prompt-unavailable")).to_be_hidden()

    page.reload()  # the key is kept by the server, not by the page
    expect(page.get_by_role("button", name="Generate plan")).to_be_visible()
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("button", name="Remove saved key").click()
    expect(dialog.locator("#llm-status")).to_contain_text("No key")
    dialog.locator("[data-close]").click()
    expect(page.get_by_role("button", name="Find templates")).to_be_visible()
    # The browser itself logs the refused key's HTTP 400; nothing else may fail.
    assert [error for error in errors if "400 (Bad Request)" not in error] == []


def test_theme_can_be_pinned_in_settings(page: Page, live_server: LiveServer) -> None:
    page.emulate_media(color_scheme="light")
    open_home(page, live_server)
    page.get_by_role("button", name="Settings").click()
    page.locator("#theme-select").select_option("dark")
    page.reload()  # applied by theme.js before the first paint
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_role("button", name="Settings").click()
    page.locator("#theme-select").select_option("system")
    expect(page.locator("html")).not_to_have_attribute("data-theme", "dark")


def test_dashboard_keeps_every_address_and_the_sign_in_notes(
    page: Page, live_server: LiveServer
) -> None:
    errors = open_home(page, live_server)
    deploy_template(page, "Media Stack", "media")
    expect(page.locator("#status-badge")).to_have_text("Ready", timeout=20_000)

    row = page.locator(".environment", has=page.locator("code", has_text="media"))
    links = row.locator(".environment-links a")
    expect(links).to_have_count(5, timeout=15_000)
    expect(links.first).to_have_text("http://media.localhost")
    expect(row.locator(".environment-links")).to_contain_text("http://sonarr.media.localhost")
    row.get_by_text("Sign-in and notes").click()
    expect(row.locator(".environment-notes")).to_contain_text("Jellyfin opens a setup wizard")
    assert errors == []


def test_a_failed_analysis_can_be_retried(
    page: Page, live_server_unsupported: tuple[LiveServer, FakeTranslator]
) -> None:
    server, translator = live_server_unsupported
    errors = open_home(page, server)
    page.locator("#prompt-input").fill("A mail server open to the Internet")
    page.get_by_role("button", name="Generate plan").click()
    expect(page.locator("#status-badge")).to_have_text("Failed", timeout=15_000)

    page.locator("#job-result").get_by_role("button", name="Retry").click()

    expect(page.locator("#status-badge")).to_have_text("Failed", timeout=15_000)
    expect(page.locator("#job-result").get_by_role("button", name="Retry")).to_be_visible()
    assert translator.prompts == ["A mail server open to the Internet"] * 2
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


def test_a_running_deployment_can_be_cancelled(page: Page, live_server_slow: LiveServer) -> None:
    errors = open_home(page, live_server_slow)
    deploy_template(page, "Media Stack", "media")
    cancel = page.locator("#job-cancel")
    expect(cancel).to_be_visible()

    cancel.click()

    expect(page.locator("#status-badge")).to_have_text("Cancelled", timeout=20_000)
    expect(page.locator("#job-result")).to_contain_text("cancelled")
    expect(page.locator("#job-result").get_by_role("button", name="Start again")).to_be_visible()
    expect(cancel).to_be_hidden()
    assert errors == []


def test_dashboard_lists_services_and_restarts_one(page: Page, live_server: LiveServer) -> None:
    errors = open_home(page, live_server)
    deploy_template(page, "Media Stack", "media")
    expect(page.locator("#status-badge")).to_have_text("Ready", timeout=20_000)

    title = page.locator(".environment-title code", has_text="media")
    row = page.locator(".environment", has=title)
    row.get_by_text("Services (5)").click()
    restart = row.get_by_role("button", name="Restart Sonarr 4 in media")
    expect(restart).to_be_enabled(timeout=15_000)
    expect(row.locator(".service-states")).to_contain_text("Running")
    restart.click()

    expect(page.locator("#job-title")).to_have_text("Restart of sonarr in media")
    expect(page.locator("#status-badge")).to_have_text("Running", timeout=15_000)
    assert errors == []


def test_setup_check_shows_on_first_visit_until_dismissed(
    page: Page, live_server: LiveServer
) -> None:
    errors = open_home(page, live_server)
    panel = page.locator("#setup-check")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("Simulated engine")
    expect(panel).to_contain_text("AI plans are off")

    page.get_by_role("button", name="Got it").click()
    expect(panel).to_be_hidden()
    page.reload()
    expect(page.locator(".template-card").first).to_be_visible()
    expect(panel).to_be_hidden()
    assert errors == []


def test_template_dialog_shows_what_the_deployment_needs(
    page: Page, live_server: LiveServer
) -> None:
    errors = open_home(page, live_server)
    page.get_by_role("button", name="Details of Media Stack").click()
    dialog = page.locator("#template-dialog")
    expect(dialog).to_contain_text("About 1.0 GB to download (5 of 5 images)")
    expect(dialog).to_contain_text("Needs about 750 MB of memory · 6.0 GB available")
    page.locator("#project-name").fill("media")
    page.locator("#dialog-deploy").click()
    expect(page.locator("#status-badge")).to_have_text("Ready", timeout=20_000)

    page.get_by_role("button", name="Details of Media Stack").click()
    expect(dialog).to_contain_text("All 5 images are already downloaded")
    assert errors == []


def test_docker_desktop_disk_space_is_flagged_before_deploying(
    page: Page, live_server: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker Desktop's disk image claims about 1 TB: the dialog must not present that
    as room without saying the host drive may have less."""

    async def desktop(self: SimulatedEngine) -> HostResources:
        return HostResources(
            memory_total_mb=8192,
            memory_available_mb=6144,
            disk_free_mb=1_000_000,
            disk_is_virtual=True,
        )

    monkeypatch.setattr(SimulatedEngine, "resources", desktop)
    errors = open_home(page, live_server)
    page.get_by_role("button", name="Details of Media Stack").click()
    disk = page.locator("#template-dialog .readiness-list li", has_text="disk image")

    expect(disk).to_contain_text("check the free space of the drive that holds it")
    expect(disk).to_have_attribute("data-tone", "info")
    assert errors == []
