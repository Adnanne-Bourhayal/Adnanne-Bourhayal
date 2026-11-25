"""Capture only the approved public screens. A failed capture leaves its previous cover intact."""

import os
import re
import sys
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import expect, sync_playwright


# Nova's service catalogue excludes booking forms, customer reviews and contact details.
# The financial example must be the built-in sample ledger opened in a clean context.
SITES = {
    "smart-api": ("https://smart-api-monitor.vercel.app/", "main", "Sample requests"),
    "finflow": ("https://finflowbeta.vercel.app/", "body", "Example ledger"),
    "nova": ("https://nvdetail.vercel.app/", "section#services", "Premium Diensten"),
    "import": ("https://import-your-dream-website.vercel.app/", "section:has(h1)", "BELGIUM TO SPAIN"),
}
WIDTH, HEIGHT = 1280, 720


def capture(browser, url, selector, public_marker):
    context = browser.new_context(
        viewport={"width": WIDTH, "height": HEIGHT}, locale="en-GB",
        timezone_id="Europe/Brussels", reduced_motion="reduce", color_scheme="light",
        service_workers="block", accept_downloads=False,
    )
    try:
        host = urlsplit(url).netloc

        def public_get(route):
            request = route.request
            if request.method in ("GET", "HEAD") and urlsplit(request.url).netloc == host:
                return route.continue_()
            return route.abort()

        context.route("**/*", public_get)
        page = context.new_page()
        response = page.goto(url, wait_until="load", timeout=30_000)
        if not response or response.status != 200 or page.url.rstrip("/") != url.rstrip("/"):
            raise ValueError("The approved public landing page is unavailable")
        region = page.locator(selector).first
        expect(region.get_by_text(public_marker, exact=False).first).to_be_visible(timeout=10_000)
        if page.get_by_role("dialog").count() and page.get_by_role("dialog").first.is_visible():
            raise ValueError("An interstitial is visible")
        page.evaluate("document.fonts.ready")
        # Only the first screen is published. In particular the Nova capture must stay
        # inside the catalogue and must never include the neighbouring reviews section.
        if selector == "section#services":
            region.evaluate("""el => {
                document.documentElement.style.scrollBehavior = "auto";
                window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top);
                for (const node of document.body.querySelectorAll('*')) {
                    if (!el.contains(node) && !node.contains(el) &&
                        ['fixed','sticky'].includes(getComputedStyle(node).position))
                        node.style.visibility = 'hidden';
                }
            }""")
            page.wait_for_function("""() => {
                const section = document.querySelector('section#services');
                const r = section.getBoundingClientRect();
                return r.top >= -2 && r.top <= 2 && r.bottom >= innerHeight &&
                    Array.from(section.querySelectorAll('img')).filter(img =>
                        img.getBoundingClientRect().top < innerHeight
                    ).every(img => img.complete && img.naturalWidth > 0);
            }""", timeout=10_000)
        # Keep this fail-closed if a redesigned landing page exposes contact data,
        # fields, or a cookie banner in the photographed area.
        visible_text = page.evaluate("""() => {
            const visible = r => r.width && r.height && r.bottom > 0 && r.top < innerHeight
                && r.right > 0 && r.left < innerWidth;
            const displayed = e => getComputedStyle(e).visibility !== 'hidden' &&
                getComputedStyle(e).display !== 'none';
            const form = [...document.querySelectorAll('input:not([type=hidden]), textarea')]
                .some(e => displayed(e) && visible(e.getBoundingClientRect()));
            if (form) return null;
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const text = [];
            for (let node; (node = walker.nextNode());) {
                if (!node.textContent.trim() || !displayed(node.parentElement)) continue;
                const range = document.createRange();
                range.selectNodeContents(node);
                if (visible(range.getBoundingClientRect())) text.push(node.textContent.trim());
            }
            return text.join(' ');
        }""")
        if visible_text is None or re.search(
            r"[@]|(?:\+|00)\d[\d ()-]{7,}\d|accept all cookies|accepteer alle cookies|aceptar todas las cookies",
            visible_text, re.IGNORECASE,
        ):
            raise ValueError("The selected screen includes a form, contact details or a cookie notice")
        page.evaluate("""() => Promise.race([
            Promise.allSettled(document.getAnimations()
              .filter(a => Number.isFinite(a.effect?.getComputedTiming().endTime))
              .map(a => a.finished)),
            new Promise(resolve => setTimeout(resolve, 1500))
        ])""")
        pixels = page.screenshot(animations="disabled", timeout=15_000)
        with Image.open(BytesIO(pixels)) as image:
            image = image.convert("RGB")
            if image.size != (WIDTH, HEIGHT) or all(b - a < 20 for a, b in image.getextrema()):
                raise ValueError("Unexpected capture dimensions or empty image")
            output = BytesIO()
            image.save(output, "WEBP", quality=82, method=6)
            return output.getvalue()
    finally:
        context.close()


def stable_bytes(data, previous):
    if not previous or not previous.is_file():
        return data
    old = previous.read_bytes()
    with Image.open(BytesIO(data)) as current, Image.open(BytesIO(old)) as baseline:
        if current.size != baseline.size:
            return data
        difference = ImageChops.difference(current.convert("RGB"), baseline.convert("RGB")).convert("L")
        # Ignore tiny encoder/antialias differences. Changed text or a new layout is retained.
        if ImageStat.Stat(difference).mean[0] < 1 and sum(difference.histogram()[32:]) < 40:
            return old
    return data


def main(destination, baseline=None):
    destination.mkdir(parents=True, exist_ok=True)
    failures = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("PREVIEW_CHROME"))
        for name, settings in SITES.items():
            try:
                data = capture(browser, *settings)
                filename = name + ".webp"
                prior = baseline / filename if baseline else None
                (destination / filename).write_bytes(stable_bytes(data, prior))
                print(f"Captured {name}", flush=True)
            except (BrowserError, ValueError, AssertionError) as exc:
                print(f"Skipped {name} ({type(exc).__name__}). Keeping its last published preview.", flush=True)
                failures.append(name)
        browser.close()
    if failures:
        print("Incomplete captures: " + ", ".join(failures), flush=True)
    if len(failures) == len(SITES):
        raise SystemExit("No approved public screen could be captured.")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
