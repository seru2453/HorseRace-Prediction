const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8010",
    channel: "chrome",
    screenshot: "only-on-failure"
  },
  webServer: {
    command: "AUTO_REVIEW_ENABLED=0 DATABASE_URL=sqlite:////tmp/horseprediction-playwright.sqlite3 PORT=8010 make dev",
    url: "http://127.0.0.1:8010",
    reuseExistingServer: true,
    timeout: 30_000
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 900 } } },
    { name: "mobile", use: { viewport: { width: 390, height: 844 }, isMobile: true } }
  ]
});
