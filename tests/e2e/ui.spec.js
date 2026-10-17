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

test("analysis is visual and reload restores the race and tab", async ({ page }, testInfo) => {
  await stubSchedule(page);
  await page.route("**/api/analyze", async (route) => {
    const payload = JSON.parse(route.request().postData() || "{}");
    await new Promise((resolve) => setTimeout(resolve, 300));
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
  const analyzeButton = page.getByRole("button", { name: "分析開始" });
  await analyzeButton.click();
  await expect(page.getByRole("button", { name: "分析中" })).toBeDisabled();
  await expect(page.locator("#progress .spinner")).toBeVisible();

  const predictions = page.locator("#analysis .prediction");
  await expect(predictions.first()).toBeVisible();
  await expect(page.getByRole("button", { name: "分析開始" })).toBeEnabled();
  expect(await predictions.count()).toBe(await page.locator("#entries .row").count());
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
  await expect(page.locator(".tabs button.active")).toHaveText("自分の予想");
  await expect(page.locator("#race-date")).toHaveValue("2026-08-16");
  await expect(page.locator("#racecourse")).toHaveValue("札幌");
  await expect(page.locator("#race-number")).toHaveValue("11");

  const reloadScreenshot = await page.screenshot({
    path: `test-results/ui-reload-${testInfo.project.name}.png`,
    fullPage: true
  });
  expect(reloadScreenshot.length).toBeGreaterThan(10_000);
  const bodyWidth = await page.locator("body").evaluate((element) => element.scrollWidth);
  expect(bodyWidth).toBeLessThanOrEqual(testInfo.project.use.viewport.width);
});

test("a previous-day selection is restored after reload", async ({ page }) => {
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
  await expect(page.locator("#race-date")).toHaveValue("2026-08-29");
  await expect(page.locator("#racecourse")).toHaveValue("札幌");
  await expect(page.locator("#race-number")).toHaveValue("11");
  await expect(page.locator("#tabs")).toBeHidden();
});

test("a changed race date is not overwritten by the previous analysis", async ({ page }) => {
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
  await expect(page.locator("#race-date")).toHaveValue("2026-09-12");
  await expect(page.locator("#racecourse")).toHaveValue("阪神");
  await expect(page.locator("#race-number")).toHaveValue("11");
  await expect(page.locator("#tabs")).toBeHidden();
});

test("race selection follows the daily schedule and falls back to manual options", async ({ page }) => {
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
  await expect(page.locator("#error")).toContainText("手動選択に切り替えています");
  await expect(page.locator("#racecourse option")).toHaveCount(10);
  await expect(page.locator("#race-number option")).toHaveCount(12);
});

test("local admin actions are available from the compact accessible dot menu", async ({ page }) => {
  await page.goto("/");

  const menu = page.locator("#local-admin-tools");
  const trigger = menu.locator("summary");
  await expect(trigger).toHaveText("•••");
  await expect(trigger).toHaveAttribute("aria-label", "その他の操作");
  await expect(trigger).toHaveAttribute("title", "その他の操作");
  await expect(trigger).toBeVisible();
  await expect.poll(async () => (await trigger.boundingBox())?.width).toBe(44);

  await trigger.click();
  await expect(menu.getByRole("button", {name: "取得診断"})).toBeVisible();
  await expect(menu.getByRole("button", {name: "データ出力"})).toBeVisible();
  await expect(menu.getByRole("button", {name: "データ読込"})).toBeVisible();
});
