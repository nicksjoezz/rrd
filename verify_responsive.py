import time
from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # Test mobile view
        page.set_viewport_size({"width": 375, "height": 667})
        page.goto("http://localhost:5000")
        time.sleep(1)
        page.screenshot(path="mobile_dashboard.png")

        # Test sidebar toggle
        page.click("#mobile-nav-toggle")
        time.sleep(0.5)
        page.screenshot(path="mobile_sidebar.png")

        # Test tablet view
        page.set_viewport_size({"width": 768, "height": 1024})
        time.sleep(1)
        page.screenshot(path="tablet_dashboard.png")

        # Test desktop view
        page.set_viewport_size({"width": 1280, "height": 800})
        time.sleep(1)
        page.screenshot(path="desktop_dashboard.png")

        browser.close()

if __name__ == "__main__":
    import subprocess
    import sys
    # Start server in background
    server = subprocess.Popen(["python3", "main.py", "--no-bot"])
    time.sleep(3) # Wait for server to start
    try:
        run()
    finally:
        server.terminate()
