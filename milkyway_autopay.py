#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import gc
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright

LOG = logging.getLogger("milkyway_autopay")

TEST_CARD = "4242424242424242"
TEST_EXP = "1234"      # 12/34
TEST_CVC = "123"
TEST_NAME = "Test User"
TEST_POSTAL = "10001"

SUCCESS_PATTERNS = [
    re.compile(r"Payment successful", re.I),
    re.compile(r"Thanks|Thank you", re.I),
    re.compile(r"Complete|Completed", re.I),
    re.compile(r"Success", re.I),
]


@dataclass(slots=True)
class AccountConfig:
    name: str
    game_url: str
    state_file: str
    prep_bias_ms: int = 0
    package_timeout_sec: int = 12


@dataclass(slots=True)
class RunSettings:
    loops: int = 100
    quantity: int = 99
    delay_sec: float = 0.1
    max_inflight: int = 2
    refresh_every: int = 50
    recycle_context_every: int = 250
    final_drain_timeout: int = 120
    save_failure_screenshots: bool = False
    block_assets: bool = True
    browser_channel: str | None = None
    results_csv: str = "autopay_results.csv"
    log_every: int = 20
    debug_dir: str = "autopay_debug"


class CsvSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        self.header_written = self.path.exists() and self.path.stat().st_size > 0

    async def write_row(self, row: dict[str, Any]) -> None:
        async with self.lock:
            fieldnames = list(row.keys())
            write_header = not self.header_written
            with self.path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if write_header:
                    writer.writeheader()
                    self.header_written = True
                writer.writerow(row)


@dataclass(slots=True)
class PendingCheckout:
    page: Page
    submitted_at: float


@dataclass(slots=True)
class WorkerSummary:
    account: str
    total: int = 0
    ok: int = 0
    failed: int = 0


async def save_state_from_existing_chrome(cdp_url: str, output_path: str) -> None:
    """Attach to an already-open Chromium browser and persist auth state."""
    p = await async_playwright().start()
    try:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError(f"No browser context found at {cdp_url}")
        context = browser.contexts[0]
        await context.storage_state(path=output_path, indexed_db=True)
        LOG.info("Saved storage state to %s", output_path)
    finally:
        await p.stop()


def load_accounts(accounts_path: str | Path) -> list[AccountConfig]:
    data = json.loads(Path(accounts_path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        raw_accounts = data.get("accounts", [])
    elif isinstance(data, list):
        raw_accounts = data
    else:
        raise ValueError("accounts file must be a list or a dict with an 'accounts' key")

    accounts: list[AccountConfig] = []
    for item in raw_accounts:
        accounts.append(AccountConfig(
            name=item["name"],
            game_url=item["game_url"],
            state_file=item["state_file"],
            prep_bias_ms=int(item.get("prep_bias_ms", 0)),
            package_timeout_sec=int(item.get("package_timeout_sec", 12)),
        ))
    if not accounts:
        raise ValueError("no accounts found in accounts file")
    return accounts


async def first_visible_locator(locators: list[Any], timeout: float = 5.0):
    end = time.time() + timeout
    while time.time() < end:
        for loc in locators:
            try:
                if await loc.count() > 0 and await loc.first.is_visible():
                    return loc.first
            except Exception:
                pass
        await asyncio.sleep(0.12)
    return None


async def dismiss_offline_progress_modal(page: Page, timeout_ms: int = 2500) -> bool:
    bg = page.locator('div[class*="OfflineProgressModal_background"]').first
    modal = page.locator('div[class*="OfflineProgressModal_modalContainer"]').first

    try:
        bg_visible = await bg.is_visible()
    except Exception:
        bg_visible = False
    try:
        modal_visible = await modal.is_visible()
    except Exception:
        modal_visible = False

    if not (bg_visible or modal_visible):
        return False

    btn = await first_visible_locator([
        modal.get_by_role("button", name=re.compile(r"close|ok|collect|claim|x|×", re.I)),
        modal.get_by_text(re.compile(r"close|ok|collect|claim|x|×", re.I)),
        modal.locator("button").first,
    ], timeout=0.8)

    if btn is not None:
        try:
            await btn.click()
        except Exception:
            pass
    else:
        try:
            if bg_visible:
                await bg.click()
        except Exception:
            pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    end = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < end:
        try:
            still_bg = await bg.is_visible()
        except Exception:
            still_bg = False
        try:
            still_modal = await modal.is_visible()
        except Exception:
            still_modal = False
        if not (still_bg or still_modal):
            return True
        await asyncio.sleep(0.1)

    return False


async def install_window_open_capture(page: Page) -> None:
    await page.evaluate(
        """
        () => {
            if (window.__mw_open_capture_installed) return;
            window.__mw_open_capture_installed = true;
            window.__mw_last_open_url = null;
            const originalOpen = window.open.bind(window);
            window.__mw_original_open = originalOpen;
            window.open = function(url, ...args) {
                window.__mw_last_open_url = url;
                return null;
            };
        }
        """
    )


async def wait_for_checkout_url(game_page: Page, timeout_ms: int = 4000) -> str | None:
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        try:
            url = await game_page.evaluate("window.__mw_last_open_url")
            if url and "checkout.stripe.com" in url:
                return url
        except Exception:
            pass
        await asyncio.sleep(0.08)
    return None


async def maybe_click_with_intercept_retry(page: Page, locator: Any) -> None:
    try:
        await locator.click(timeout=5000)
    except Exception as e:
        if "intercepts pointer events" in str(e):
            await dismiss_offline_progress_modal(page, timeout_ms=2500)
            await locator.click(timeout=5000)
        else:
            raise


async def enter_store_and_get_region(game_page: Page, timeout_ms: int = 15000) -> Any:
    store_btn = game_page.get_by_text(re.compile(r"^Cowbell Store$", re.I)).first
    buy_tab = game_page.get_by_text(re.compile(r"^Buy Cowbells$", re.I)).first

    await store_btn.wait_for(state="visible", timeout=timeout_ms)
    await maybe_click_with_intercept_retry(game_page, store_btn)

    await buy_tab.wait_for(state="visible", timeout=timeout_ms)
    await maybe_click_with_intercept_retry(game_page, buy_tab)

    region_candidates = [
        game_page.locator("section").filter(has_text=re.compile(r"Buy Cowbells", re.I)).first,
        game_page.locator("div").filter(has_text=re.compile(r"Buy Cowbells", re.I)).first,
        game_page.locator("main").first,
    ]

    for reg in region_candidates:
        try:
            if await reg.count() > 0 and await reg.first.is_visible():
                return reg.first
        except Exception:
            pass
    return game_page


async def wait_for_100_package_in_region(region: Any, timeout_sec: float) -> Any:
    candidates = [
        region.get_by_text(re.compile(r"11500\s+Cowbells", re.I)).first,
        region.get_by_text(re.compile(r"Price:\s*\$100", re.I)).first,
        region.get_by_role("button", name=re.compile(r"11500\s+Cowbells", re.I)).first,
        region.get_by_role("button", name=re.compile(r"Price:\s*\$100", re.I)).first,
    ]
    return await first_visible_locator(candidates, timeout=timeout_sec)


async def new_checkout_worker_tab(context: BrowserContext) -> Page:
    page = await context.new_page()
    await page.goto("about:blank")
    return page


async def open_100_dollar_package(
    game_page: Page,
    *,
    package_timeout_sec: int,
    prep_bias_ms: int = 0,
    max_attempts: int = 3,
    just_reloaded: bool = False,
) -> Page:
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            if just_reloaded and attempt == 1:
                if prep_bias_ms > 0:
                    await game_page.wait_for_timeout(prep_bias_ms)
                await dismiss_offline_progress_modal(game_page, timeout_ms=2500)

            region = await enter_store_and_get_region(game_page, timeout_ms=15000)
            package_el = await wait_for_100_package_in_region(region, timeout_sec=package_timeout_sec + attempt * 2)
            if package_el is None:
                raise RuntimeError("$100 package not visible in store region")

            await maybe_click_with_intercept_retry(game_page, package_el)

            continue_btn = game_page.get_by_role("button", name=re.compile(r"^Continue$", re.I)).first
            modal_title = game_page.get_by_text(re.compile(r"Continue to Purchase", re.I)).first
            await modal_title.wait_for(state="visible", timeout=8000)
            await continue_btn.wait_for(state="visible", timeout=8000)

            await install_window_open_capture(game_page)
            await continue_btn.click()
            checkout_url = await wait_for_checkout_url(game_page, timeout_ms=4000)
            if not checkout_url or "checkout.stripe.com" not in checkout_url:
                raise RuntimeError(f"failed to capture checkout URL: {checkout_url}")

            checkout_page = await new_checkout_worker_tab(game_page.context)
            await checkout_page.goto(checkout_url, wait_until="domcontentloaded")
            return checkout_page

        except Exception as e:
            last_error = e
            if "intercepts pointer events" in str(e):
                await dismiss_offline_progress_modal(game_page, timeout_ms=2500)
                just_reloaded = False
                continue
            if attempt < max_attempts:
                await game_page.wait_for_timeout(500 + 250 * attempt)

    raise RuntimeError(f"open_100_dollar_package failed after {max_attempts} attempts: {last_error}")


async def set_quantity_on_checkout(checkout_page: Page, target: int = 99) -> None:
    qty_btn = checkout_page.get_by_role("button", name=re.compile(r"^Qty\s+\d+$")).first
    await qty_btn.wait_for(state="visible", timeout=10000)
    await qty_btn.click()

    qty_input = await first_visible_locator([
        checkout_page.get_by_role("dialog").locator("input"),
        checkout_page.locator("[aria-modal='true'] input"),
        checkout_page.locator("input:focus"),
    ], timeout=5)
    if qty_input is None:
        raise RuntimeError("quantity input not found in qty dialog")

    await qty_input.click()
    try:
        await qty_input.press("Meta+A")
    except Exception:
        pass
    await qty_input.fill(str(target))

    update_btn = checkout_page.get_by_role("button", name=re.compile(r"^Update$", re.I)).first
    await update_btn.click()
    await checkout_page.get_by_role("button", name=re.compile(rf"^Qty\s+{target}$")).first.wait_for(
        state="visible", timeout=8000
    )


async def first_visible(candidates: list[Any], timeout_ms: int = 1500):
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        for loc in candidates:
            try:
                cand = loc.first
                if await cand.count() > 0 and await cand.is_visible():
                    return cand
            except Exception:
                pass
        await asyncio.sleep(0.12)
    return None


async def clear_and_fill(locator: Any, value: str) -> None:
    await locator.click()
    try:
        await locator.press("Meta+A")
    except Exception:
        pass
    try:
        await locator.press("Control+A")
    except Exception:
        pass
    await locator.fill(value)


async def fill_if_needed(locator: Any, value: str, skip_if_nonempty: bool = True) -> bool:
    if locator is None:
        return False
    try:
        current = await locator.input_value()
    except Exception:
        current = ""
    if skip_if_nonempty and current.strip():
        return True
    await clear_and_fill(locator, value)
    return True


@dataclass(slots=True)
class FastManualFields:
    pay_without_link: Any = None
    submit: Any = None
    card_number: Any = None
    expiry: Any = None
    cvc: Any = None
    cardholder: Any = None
    postal: Any = None


async def resolve_on_target(target: Any, kind: str, timeout_ms: int = 1500):
    if kind == "pay_without_link":
        return await first_visible([
            target.get_by_role("button", name=re.compile(r"Pay without Link", re.I)),
            target.get_by_text(re.compile(r"Pay without Link", re.I)),
        ], timeout_ms=timeout_ms)

    if kind == "submit":
        return await first_visible([
            target.locator("button[type='submit']"),
            target.get_by_role("button", name=re.compile(r"^(Pay|Complete|Confirm|Subscribe)", re.I)),
        ], timeout_ms=timeout_ms)

    if kind == "card_number":
        return await first_visible([
            target.get_by_label(re.compile(r"Card number", re.I)),
            target.get_by_placeholder(re.compile(r"1234 1234 1234 1234", re.I)),
            target.locator('input[autocomplete="cc-number"]'),
            target.locator('input[name*="cardnumber"]'),
        ], timeout_ms=timeout_ms)

    if kind == "expiry":
        return await first_visible([
            target.get_by_label(re.compile(r"MM\s*/\s*YY|Exp(?:iry|iration)", re.I)),
            target.get_by_placeholder(re.compile(r"MM\s*/\s*YY", re.I)),
            target.locator('input[autocomplete="cc-exp"]'),
            target.locator('input[name*="exp"]'),
        ], timeout_ms=timeout_ms)

    if kind == "cvc":
        return await first_visible([
            target.get_by_label(re.compile(r"CVC|CVV|Security code", re.I)),
            target.get_by_placeholder(re.compile(r"CVC|CVV", re.I)),
            target.locator('input[autocomplete="cc-csc"]'),
            target.locator('input[name*="cvc"], input[name*="cvv"]'),
        ], timeout_ms=timeout_ms)

    if kind == "cardholder":
        return await first_visible([
            target.get_by_label(re.compile(r"Cardholder name|Name on card", re.I)),
            target.get_by_placeholder(re.compile(r"Cardholder name|Name on card", re.I)),
            target.locator('input[autocomplete="cc-name"]'),
        ], timeout_ms=timeout_ms)

    if kind == "postal":
        return await first_visible([
            target.get_by_label(re.compile(r"ZIP|Postal code|Postcode", re.I)),
            target.get_by_placeholder(re.compile(r"ZIP|Postal code|Postcode", re.I)),
            target.locator('input[autocomplete="postal-code"]'),
        ], timeout_ms=timeout_ms)

    return None


async def resolve_manual_fields_fast(checkout_page: Page) -> FastManualFields:
    fields = FastManualFields()

    fields.pay_without_link = await resolve_on_target(checkout_page, "pay_without_link", timeout_ms=1200)
    if fields.pay_without_link is not None:
        await fields.pay_without_link.click()
        await checkout_page.wait_for_timeout(600)

    fields.card_number = await resolve_on_target(checkout_page, "card_number", timeout_ms=5000)
    if fields.card_number is not None:
        fields.expiry = await resolve_on_target(checkout_page, "expiry", timeout_ms=800)
        fields.cvc = await resolve_on_target(checkout_page, "cvc", timeout_ms=800)
        fields.cardholder = await resolve_on_target(checkout_page, "cardholder", timeout_ms=800)
        fields.postal = await resolve_on_target(checkout_page, "postal", timeout_ms=800)

    fields.submit = await resolve_on_target(checkout_page, "submit", timeout_ms=1500)
    return fields


async def fill_manual_fields_fast(fields: FastManualFields) -> None:
    if fields.card_number is None:
        raise RuntimeError("card number field not found")

    await clear_and_fill(fields.card_number, TEST_CARD)
    if fields.expiry is not None:
        await clear_and_fill(fields.expiry, TEST_EXP)
    if fields.cvc is not None:
        await clear_and_fill(fields.cvc, TEST_CVC)
    if fields.cardholder is not None:
        await fill_if_needed(fields.cardholder, TEST_NAME, skip_if_nonempty=True)
    if fields.postal is not None:
        await fill_if_needed(fields.postal, TEST_POSTAL, skip_if_nonempty=True)


async def submit_checkout_only_fast(checkout_page: Page) -> PendingCheckout:
    fields = await resolve_manual_fields_fast(checkout_page)
    await fill_manual_fields_fast(fields)
    if fields.submit is None:
        raise RuntimeError("submit button not found")
    await fields.submit.click()
    await checkout_page.wait_for_timeout(200)
    return PendingCheckout(page=checkout_page, submitted_at=time.time())


async def checkout_is_done(page: Page) -> bool:
    if page.is_closed():
        return True
    try:
        url = page.url
    except Exception:
        return True

    if "paymentSuccess" in url:
        return True
    if "checkout.stripe.com" not in url:
        return True

    for pat in SUCCESS_PATTERNS:
        try:
            loc = page.get_by_text(pat).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:
            pass
    return False


async def reap_finished_checkouts(pending_checkouts: list[PendingCheckout]) -> tuple[list[PendingCheckout], int]:
    still_pending: list[PendingCheckout] = []
    finished = 0
    for item in pending_checkouts:
        try:
            done = await checkout_is_done(item.page)
        except Exception:
            done = True
        if done:
            try:
                if not item.page.is_closed():
                    await item.page.close()
            except Exception:
                pass
            finished += 1
        else:
            still_pending.append(item)
    return still_pending, finished


async def wait_for_checkout_slot(pending_checkouts: list[PendingCheckout], max_inflight: int) -> list[PendingCheckout]:
    while len(pending_checkouts) >= max_inflight:
        pending_checkouts, finished = await reap_finished_checkouts(pending_checkouts)
        if finished == 0:
            await asyncio.sleep(0.3)
    return pending_checkouts


async def block_static_assets(route: Route) -> None:
    rt = route.request.resource_type
    if rt in {"image", "font", "media"}:
        await route.abort()
    else:
        await route.continue_()


async def new_context_for_account(browser: Browser, account: AccountConfig, settings: RunSettings) -> tuple[BrowserContext, Page]:
    context = await browser.new_context(
        storage_state=account.state_file,
        service_workers="block",
        viewport={"width": 1280, "height": 900},
    )
    if settings.block_assets:
        await context.route("**/*", block_static_assets)
    game_page = await context.new_page()
    await game_page.goto(account.game_url, wait_until="domcontentloaded")
    return context, game_page


async def safe_close_context(context: BrowserContext | None, reason: str = "cleanup") -> None:
    if context is None:
        return
    try:
        await context.close(reason=reason)
    except Exception as e:
        msg = str(e)
        if "Connection closed" not in msg and "Target page, context or browser has been closed" not in msg:
            LOG.warning("context close warning: %s", e)


async def safe_close_browser(browser: Browser | None, reason: str = "cleanup") -> None:
    if browser is None:
        return
    try:
        if browser.is_connected():
            await browser.close(reason=reason)
    except Exception as e:
        msg = str(e)
        if "Connection closed" not in msg and "Target page, context or browser has been closed" not in msg:
            LOG.warning("browser close warning: %s", e)


async def take_failure_screenshot(page: Page | None, path: Path) -> None:
    if page is None:
        return
    try:
        if not page.is_closed():
            await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


async def account_worker(playwright: Playwright, account: AccountConfig, settings: RunSettings, sink: CsvSink) -> WorkerSummary:
    debug_dir = Path(settings.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    browser = await playwright.chromium.launch(
        headless=True,
        channel=settings.browser_channel,
    ) if settings.browser_channel else await playwright.chromium.launch(headless=True)

    context: BrowserContext | None = None
    game_page: Page | None = None
    pending_checkouts: list[PendingCheckout] = []
    summary = WorkerSummary(account=account.name)

    async def reset_context() -> None:
        nonlocal context, game_page, pending_checkouts
        if pending_checkouts:
            for item in pending_checkouts:
                try:
                    if not item.page.is_closed():
                        await item.page.close()
                except Exception:
                    pass
            pending_checkouts = []
        await safe_close_context(context, reason="reset")
        context, game_page = await new_context_for_account(browser, account, settings)

    try:
        await reset_context()

        for i in range(1, settings.loops + 1):
            t0 = time.perf_counter()
            checkout_page: Page | None = None
            just_reloaded = False

            try:
                if i == 1 or (settings.refresh_every > 0 and i % settings.refresh_every == 0):
                    assert game_page is not None
                    await game_page.goto(account.game_url, wait_until="domcontentloaded")
                    just_reloaded = True
                    await dismiss_offline_progress_modal(game_page, timeout_ms=2500)

                if settings.recycle_context_every > 0 and i > 1 and i % settings.recycle_context_every == 0 and not pending_checkouts:
                    await reset_context()
                    just_reloaded = True
                    assert game_page is not None
                    await dismiss_offline_progress_modal(game_page, timeout_ms=2500)

                pending_checkouts, _ = await reap_finished_checkouts(pending_checkouts)
                pending_checkouts = await wait_for_checkout_slot(pending_checkouts, settings.max_inflight)

                assert game_page is not None
                checkout_page = await open_100_dollar_package(
                    game_page,
                    package_timeout_sec=account.package_timeout_sec,
                    prep_bias_ms=account.prep_bias_ms,
                    max_attempts=3,
                    just_reloaded=just_reloaded,
                )
                await set_quantity_on_checkout(checkout_page, settings.quantity)
                pending_item = await submit_checkout_only_fast(checkout_page)
                pending_checkouts.append(pending_item)

                summary.total += 1
                summary.ok += 1
                row = {
                    "account": account.name,
                    "iteration": i,
                    "ok": True,
                    "mode": "submitted",
                    "error": "",
                    "pending_now": len(pending_checkouts),
                    "elapsed_sec": round(time.perf_counter() - t0, 2),
                }
                await sink.write_row(row)

                try:
                    await game_page.request_gc()  # best-effort only
                except Exception:
                    pass

                if settings.log_every > 0 and i % settings.log_every == 0:
                    LOG.info("[%s] iteration %s/%s inflight=%s", account.name, i, settings.loops, len(pending_checkouts))

            except Exception as e:
                summary.total += 1
                summary.failed += 1
                err = str(e)
                LOG.warning("[%s] FAILED at iteration %s: %s", account.name, i, err)

                if settings.save_failure_screenshots:
                    await take_failure_screenshot(game_page, debug_dir / f"{account.name}_game_fail_{i}.png")
                    await take_failure_screenshot(checkout_page, debug_dir / f"{account.name}_checkout_fail_{i}.png")

                row = {
                    "account": account.name,
                    "iteration": i,
                    "ok": False,
                    "mode": "",
                    "error": err,
                    "pending_now": len(pending_checkouts),
                    "elapsed_sec": round(time.perf_counter() - t0, 2),
                }
                await sink.write_row(row)
                await reset_context()

            await asyncio.sleep(settings.delay_sec)

        deadline = time.time() + settings.final_drain_timeout
        while pending_checkouts and time.time() < deadline:
            pending_checkouts, _ = await reap_finished_checkouts(pending_checkouts)
            if pending_checkouts:
                await asyncio.sleep(0.5)

        for item in pending_checkouts:
            try:
                if not item.page.is_closed():
                    await item.page.close()
            except Exception:
                pass

        return summary

    finally:
        await safe_close_context(context, reason="worker finished")
        await safe_close_browser(browser, reason="worker finished")
        gc.collect()


async def run_parallel(accounts: list[AccountConfig], settings: RunSettings) -> list[WorkerSummary]:
    sink = CsvSink(settings.results_csv)
    p = await async_playwright().start()
    try:
        results = await asyncio.gather(*[
            account_worker(p, account, settings, sink)
            for account in accounts
        ], return_exceptions=True)

        summaries: list[WorkerSummary] = []
        for account, res in zip(accounts, results):
            if isinstance(res, Exception):
                LOG.error("[%s] worker crashed: %s", account.name, res)
                await sink.write_row({
                    "account": account.name,
                    "iteration": "",
                    "ok": False,
                    "mode": "",
                    "error": f"worker crashed: {res}",
                    "pending_now": "",
                    "elapsed_sec": "",
                })
                summaries.append(WorkerSummary(account=account.name, total=0, ok=0, failed=1))
            else:
                summaries.append(res)
        return summaries
    finally:
        try:
            await p.stop()
        except Exception:
            pass
        gc.collect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Milky Way Idle test autopay runner")
    sub = parser.add_subparsers(dest="command", required=True)

    save_state_p = sub.add_parser("save-state", help="Save auth state from an already-open Chromium browser via CDP")
    save_state_p.add_argument("--cdp-url", required=True, help="e.g. http://localhost:9222")
    save_state_p.add_argument("--output", required=True, help="output state json path")

    run_p = sub.add_parser("run", help="Run the autopay loop")
    run_p.add_argument("--accounts", required=True, help="Path to accounts JSON file")
    run_p.add_argument("--loops", type=int, default=100)
    run_p.add_argument("--quantity", type=int, default=99)
    run_p.add_argument("--delay-sec", type=float, default=0.1)
    run_p.add_argument("--max-inflight", type=int, default=2)
    run_p.add_argument("--refresh-every", type=int, default=50)
    run_p.add_argument("--recycle-context-every", type=int, default=250)
    run_p.add_argument("--final-drain-timeout", type=int, default=120)
    run_p.add_argument("--results-csv", default="autopay_results.csv")
    run_p.add_argument("--debug-dir", default="autopay_debug")
    run_p.add_argument("--log-every", type=int, default=20)
    run_p.add_argument("--browser-channel", default=None, help="Optional Playwright browser channel, e.g. chrome")
    run_p.add_argument("--save-failure-screenshots", action=argparse.BooleanOptionalAction, default=False)
    run_p.add_argument("--block-assets", action=argparse.BooleanOptionalAction, default=True)
    run_p.add_argument("--verbose", action="store_true")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "save-state":
        await save_state_from_existing_chrome(args.cdp_url, args.output)
        return 0

    accounts = load_accounts(args.accounts)
    settings = RunSettings(
        loops=args.loops,
        quantity=args.quantity,
        delay_sec=args.delay_sec,
        max_inflight=args.max_inflight,
        refresh_every=args.refresh_every,
        recycle_context_every=args.recycle_context_every,
        final_drain_timeout=args.final_drain_timeout,
        save_failure_screenshots=args.save_failure_screenshots,
        block_assets=args.block_assets,
        browser_channel=args.browser_channel,
        results_csv=args.results_csv,
        log_every=args.log_every,
        debug_dir=args.debug_dir,
    )

    summaries = await run_parallel(accounts, settings)
    LOG.info("run finished")
    for s in summaries:
        LOG.info("[%s] total=%s ok=%s failed=%s", s.account, s.total, s.ok, s.failed)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        LOG.warning("interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
