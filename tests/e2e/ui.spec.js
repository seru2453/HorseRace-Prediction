const { test, expect } = require("@playwright/test");

async function stubSchedule(page) {
  await page.route("**/api/schedule?*", async (route) => {
    const date = new URL(route.request().url()).searchParams.get("date");
    const racecourse = date === "2026-09-12" ? "阪神" : "札幌";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        race_date: date,
        racecourses: [{
          name: racecourse,
          races: Array.from({length: 12}, (_, index) => ({
            race_number: index + 1,
            start_time: `10:${String(index * 5).padStart(2, "0")}`,
            race_type: "flat"
          }))
        }]
      })
    });
  });
}

test("analysis is visual and reopening defaults race date to today", async ({ page }, testInfo) => {
  await stubSchedule(page);
  await page.route("**/api/analyze", async (route) => {
    const payload = JSON.parse(route.request().postData() || "{}");
    await new Promise((resolve) => setTimeout(resolve, 800));
    const response = await route.fetch({ postData: JSON.stringify({ ...payload, mode: "demo" }) });
    const body = await response.json();
    body.predictions[0].expected_value_index = 1.6;
    body.predictions[0].expected_value_is_adjusted = true;
    await route.fulfill({ response, json: body });
  });
  const resultEntries = [
    { horse_number: 1, horse_name: "テストホース1", finish_position: 2, result_status: "finished" },
    { horse_number: 2, horse_name: "テストホース2", finish_position: 1, result_status: "finished" }
  ];
  await page.route("**/api/results/*", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, result: { entries: resultEntries }, result_source: "provider" })
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, result: { entries: resultEntries }, result_source: "provider" })
    });
  });
  await page.route("**/api/review/*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ai_brier_score: 0.1,
        ai_log_loss: 0.2,
        prediction_source: "snapshot",
        user_prediction_source: "none",
        result_source: "provider",
        note: "reviewed",
        comparison: null,
        insights: {},
        rows: resultEntries.map((entry) => ({ ...entry, ai_probability: 0.1 }))
      })
    });
  });

  await page.goto("/");
  await page.locator("#race-date").fill("2026-08-16");
  await page.locator("#racecourse").selectOption("札幌");
  await page.locator("#race-number").selectOption("11");
  const analyzeButton = page.locator(".analyze-button");
  await analyzeButton.click();
  await expect(analyzeButton).toBeDisabled();
  await expect(page.locator("#progress .spinner")).toBeVisible();
  await expect(page.locator("#progress")).toContainText("レース分析を実行中です");
  await expect(page.locator("#progress")).not.toContainText(/レース確認|出馬表取得|出走馬解析|過去データ照合|予測準備/);

  const predictions = page.locator("#analysis .prediction");
  await expect(predictions.first()).toBeVisible();
  await expect(page.locator("#tabs button")).toHaveCount(6);
  await expect(page.locator("#tabs")).toHaveCSS("flex-direction", "row");
  await expect(page.getByRole("button", { name: "分析開始" })).toBeEnabled();
  expect(await predictions.count()).toBe(await page.locator("#entries .analysis-card").count());
  await expect(page.locator(".tabs button.active")).toHaveText("AI予想");
  await expect(predictions.first().locator(".prediction-rank small")).toHaveText("本命");
  await expect(predictions.first().locator(".probability-bar span")).toBeVisible();
  await expect(predictions.first().locator(".expected-value")).toHaveText("補正期待値 1.60");
  await expect(predictions.first().locator(".factors")).not.toBeVisible();

  await predictions.first().locator(".reason-details summary").click();
  await expect(predictions.first().locator(".factors")).toBeVisible();
  await page.getByRole("button", { name: "結果", exact: true }).click();
  await expect(page.locator("[data-result-status]")).toContainText("結果取得済み");
  await expect(page.locator('.result-entry[data-horse-number="1"] [data-result-position]')).toHaveText("2着");
  await expect(page.getByText("保存済みのJRA結果を反映しました。")).toBeVisible();
  await expect(page.locator("#result-grid .result-entry").first()).toHaveAttribute("data-horse-number", "2");
  await page.getByRole("button", { name: "結果を取得", exact: true }).click();
  await expect(page.getByText("JRA公式結果を取得して保存しました。")).toBeVisible();
  await page.getByRole("button", { name: "結果を更新してレビュー" }).click();
  await expect(page.locator('.result-entry[data-horse-number="1"] [data-result-position]')).toHaveText("2着");
  await expect(page.locator('.result-entry[data-horse-number="2"] [data-result-position]')).toHaveText("1着");
  await expect(page.getByRole("button", { name: /手入力/ })).toHaveCount(0);
  const analysisScreenshot = await page.screenshot({
    path: `test-results/ui-analysis-${testInfo.project.name}.png`,
    fullPage: true
  });
  expect(analysisScreenshot.length).toBeGreaterThan(10_000);
  await page.getByRole("button", { name: "自分の予想" }).click();
  await expect(page.locator(".tabs button.active")).toHaveText("自分の予想");

  await page.evaluate(() => {
    for (const key of ["horseprediction:ui-state", "horseprediction:last-analysis"]) {
      const value = JSON.parse(localStorage.getItem(key));
      value.saved_on = "2000-01-01";
      localStorage.setItem(key, JSON.stringify(value));
    }
  });

  await page.reload();
  const today = await page.evaluate(() => {
    const date = new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  });
  await expect(page.locator("#tabs")).toBeHidden();
  await expect(page.locator("#race-date")).toHaveValue(today);
  await expect(page.locator("#racecourse")).toHaveValue("札幌");
  await expect(page.locator("#race-number")).toHaveValue(/.+/);

  const reloadScreenshot = await page.screenshot({
    path: `test-results/ui-reload-${testInfo.project.name}.png`,
    fullPage: true
  });
  expect(reloadScreenshot.length).toBeGreaterThan(10_000);
  const bodyWidth = await page.locator("body").evaluate((element) => element.scrollWidth);
  expect(bodyWidth).toBeLessThanOrEqual(testInfo.project.use.viewport.width);
});

test("started race opens the results view without creating a prediction", async ({ page }) => {
  await stubSchedule(page);
  const resultOnlyPayload = {
    race: {
      race_id: "20260816-sapporo-11",
      race_date: { value: "2026-08-16" },
      racecourse: { value: "札幌" },
      race_number: { value: 11 },
      race_name: { value: "テスト記念" },
      surface: { value: "芝" },
      distance: { value: 2000 }
    },
    entries: [],
    predictions: [],
    simulation: null,
    result: {
      result_timestamp: "2026-08-16T15:00:00+09:00",
      entries: [{ horse_number: 1, horse_name: "テストホース", finish_position: 1, result_status: "finished" }]
    },
    result_source: "provider",
    result_only: true
  };
  await page.route("**/api/analyze", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(resultOnlyPayload)
  }));

  await page.goto("/");
  await page.locator("#race-date").fill("2026-08-16");
  await page.locator("#racecourse").selectOption("札幌");
  await page.locator("#race-number").selectOption("11");
  await page.getByRole("button", { name: "分析開始" }).click();

  await expect(page.locator(".tabs button.active")).toHaveText("結果");
  await expect(page.locator("#summary")).toContainText("予測を省略");
  await expect(page.locator("[data-result-status]")).toContainText("結果取得済み");
  await expect(page.locator('.result-entry[data-horse-number="1"] [data-result-position]')).toHaveText("1着");
});

test("learning history shows the Top5 evaluation summary", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "履歴" }).click();
  const learning = page.locator("#learning");
  await expect(learning).toContainText("AI Top5評価");
  await expect(learning).toContainText("評価レース");
  await expect(learning).toContainText("標本少（100レース未満）");
});

test("opening with a previous-day selection defaults to today and today's racecourses", async ({ page }) => {
  await stubSchedule(page);
  await page.addInitScript(() => {
    localStorage.setItem("horseprediction:ui-state", JSON.stringify({
      race_date: "2026-08-29",
      racecourse: "札幌",
      race_number: "11",
      active_tab: "analysis",
      saved_on: "2026-08-29"
    }));
    localStorage.setItem("horseprediction:last-analysis", JSON.stringify({
      race_id: "20260829-札幌-11",
      request: { race_date: "2026-08-29", racecourse: "札幌", race_number: 11 },
      saved_on: "2026-08-29"
    }));
  });

  await page.goto("/");
  const today = await page.evaluate(() => {
    const date = new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  });
  await expect(page.locator("#race-date")).toHaveValue(today);
  await expect(page.locator("#racecourse option")).toHaveText(today === "2026-09-12" ? ["阪神"] : ["札幌"]);
  await expect(page.locator("#racecourse")).toHaveValue(today === "2026-09-12" ? "阪神" : "札幌");
  await expect(page.locator("#race-number")).toHaveValue(/.+/);
  await expect(page.locator("#tabs")).toBeHidden();
});

test("a saved selection from another date does not override today's default", async ({ page }) => {
  await stubSchedule(page);
  await page.addInitScript(() => {
    localStorage.setItem("horseprediction:ui-state", JSON.stringify({
      race_date: "2026-09-12",
      racecourse: "阪神",
      race_number: "11",
      active_tab: "review",
      saved_on: "2026-09-13"
    }));
    localStorage.setItem("horseprediction:last-analysis", JSON.stringify({
      race_id: "20260913-阪神-11",
      request: { race_date: "2026-09-13", racecourse: "阪神", race_number: 11 },
      saved_on: "2026-09-13"
    }));
  });

  await page.goto("/");
  const today = await page.evaluate(() => {
    const date = new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  });
  await expect(page.locator("#race-date")).toHaveValue(today);
  await expect(page.locator("#racecourse option")).toHaveText(today === "2026-09-12" ? ["阪神"] : ["札幌"]);
  await expect(page.locator("#racecourse")).toHaveValue(today === "2026-09-12" ? "阪神" : "札幌");
  await expect(page.locator("#race-number")).toHaveValue(/.+/);
  await expect(page.locator("#tabs")).toBeHidden();
});

test("race selection follows the daily schedule and stays empty if the schedule is unavailable", async ({ page }) => {
  await page.route("**/api/schedule?*", async (route) => {
    const date = new URL(route.request().url()).searchParams.get("date");
    if (date === "2026-09-12") {
      await route.fulfill({status: 503, contentType: "application/json", body: JSON.stringify({message: "schedule unavailable"})});
      return;
    }
    const racecourses = date === "2026-08-30"
      ? [
          {name: "東京", races: [{race_number: 2, start_time: "10:35", race_type: "flat"}, {race_number: 11, start_time: "15:45", race_type: "flat"}]},
          {name: "京都", races: [{race_number: 1, start_time: "09:55", race_type: "obstacle"}]}
        ]
      : [{name: "札幌", races: [{race_number: 11, start_time: "15:25", race_type: "flat"}]}];
    await route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({race_date: date, racecourses})});
  });

  await page.goto("/");
  await page.locator("#race-date").fill("2026-08-30");
  await expect(page.locator("#racecourse option")).toHaveText(["東京", "京都"]);
  await page.locator("#racecourse").selectOption("東京");
  await expect(page.locator("#race-number option")).toHaveText(["2R 10:35", "11R 15:45"]);

  await page.locator("#race-date").fill("2026-09-12");
  await expect(page.locator("#error")).toContainText("schedule unavailable");
  await expect(page.locator("#racecourse option")).toHaveText("開催情報なし");
  await expect(page.locator("#racecourse")).toHaveValue("");
  await expect(page.locator("#race-number option")).toHaveText("--");
});

test("local admin actions are available from the compact accessible menu", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#race-form")).toHaveClass(/card/);
  await expect(page.locator("#open-history")).toHaveClass(/btn/);
  await expect(page.locator(".app-mascot")).toHaveCount(0);

  const menu = page.locator("#local-admin-tools");
  const trigger = menu.locator("summary");
  await expect(trigger).toHaveText("⋮");
  await expect(trigger).toHaveAttribute("aria-label", "その他の操作");
  await expect(trigger).toHaveAttribute("title", "その他の操作");
  await expect(trigger).toBeVisible();
  await expect.poll(async () => (await trigger.boundingBox())?.width).toBe(40);
  await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  await trigger.click();
  await expect(menu.getByRole("button", {name: "取得診断"})).toBeVisible();
  await expect(menu.getByRole("button", {name: "データ出力"})).toBeVisible();
  await expect(menu.getByRole("button", {name: "データ読込"})).toBeVisible();
  await expect(menu.locator("button")).toHaveCount(3);
});

test("Material Kit dashboard uses a readable system font and light palette", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator('link[href="/vendor/material-kit.min.css"]')).toHaveCount(1);
  await expect(page.locator(":root")).toHaveCSS("color-scheme", "light");
  await expect.poll(async () => page.locator(":root").evaluate((node) => getComputedStyle(node).getPropertyValue("--bg").trim())).toBe("#f7f8fa");
  await expect.poll(async () => page.locator("body").evaluate((node) => getComputedStyle(node).fontFamily.includes("system-ui"))).toBe(true);
  await expect(page.locator("#race-date")).toHaveClass(/form-control/);
  await expect(page.locator(".app-mascot")).toHaveCount(0);

  await page.emulateMedia({ colorScheme: "dark" });
  await expect.poll(async () => page.locator(":root").evaluate((node) => getComputedStyle(node).getPropertyValue("--bg").trim())).toBe("#f7f8fa");
});

test("mobile layout and admin menu fit a 320px viewport", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto("/");
  await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);

  const menu = page.locator("#local-admin-tools");
  await menu.locator("summary").click();
  const panel = await menu.locator(".utility-menu-panel").boundingBox();
  expect(panel).not.toBeNull();
  expect(panel.x).toBeGreaterThanOrEqual(0);
  expect(panel.x + panel.width).toBeLessThanOrEqual(320);
});
