import os, time, json
from playwright.sync_api import sync_playwright

def run_cuj(page):
    # App is running on 5003
    page.goto("http://localhost:5003")
    page.wait_for_timeout(2000)

    # Check for 404/405 errors in console
    page.on("console", lambda msg: print(f"CONSOLE: {msg.type}: {msg.text}"))

    # 1. Verify /api/mode (GET)
    print("Verifying /api/mode...")
    response = page.request.get("http://localhost:5003/api/mode")
    print(f"Status: {response.status}, JSON: {response.json()}")
    assert response.status == 200
    assert "mode" in response.json()

    # 2. Verify /api/profit-history
    print("Verifying /api/profit-history...")
    response = page.request.get("http://localhost:5003/api/profit-history")
    print(f"Status: {response.status}, JSON: {response.json()}")
    assert response.status == 200

    # 3. Verify /api/logs
    print("Verifying /api/logs...")
    response = page.request.get("http://localhost:5003/api/logs?lines=10")
    print(f"Status: {response.status}, JSON: {response.json()}")
    assert response.status == 200
    assert "logs" in response.json()

    # 4. Verify Dashboard Dashboard screenshot
    print("Capturing Dashboard...")
    page.get_by_role("link", name="Dashboard").click()
    page.wait_for_timeout(1000)
    page.screenshot(path="/home/jules/verification/screenshots/dashboard_final.png")

    # 5. Verify Terminal (start bot and check logs)
    print("Capturing Terminal...")
    page.get_by_role("link", name="Terminal").click()
    page.wait_for_timeout(1000)
    page.get_by_text("START BOT").click()
    page.wait_for_timeout(3000)
    page.screenshot(path="/home/jules/verification/screenshots/terminal_final.png")

if __name__ == "__main__":
    os.makedirs("/home/jules/verification/videos", exist_ok=True)
    os.makedirs("/home/jules/verification/screenshots", exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            record_video_dir="/home/jules/verification/videos"
        )
        page = context.new_page()
        try:
            run_cuj(page)
        finally:
            context.close()
            browser.close()
