import { describe, expect, it } from "vitest";
import { TestDriver } from "testdriverai/vitest/hooks";

describe("Sidebar Platform Shortcuts", () => {
  it("should navigate to platform cards matching sidebar shortcuts", async (context) => {
    const testdriver = TestDriver(context);

    const baseUrl = process.env.APP_URL || "https://elliott-queensland-unknown-identified.trycloudflare.com";
    await testdriver.provision.chrome({ url: `${baseUrl}/accounts/login/` });

    await testdriver.find("Email input field").click();
    await testdriver.type("testuser@example.com");
    await testdriver.find("Password input field").click();
    await testdriver.type("TestPass123!");
    await testdriver.find("Log in button").click();

    await testdriver.find("TikTok shortcut in sidebar under Connect channels", { timeout: 15000 }).then(el => el.click());
    await testdriver.find("TikTok platform card", { timeout: 15000 });
    const assertTikTok = await testdriver.assert("the TikTok platform card is visible on screen");
    expect(assertTikTok).toBeTruthy();

    await testdriver.find("More channels link in sidebar under Connect channels", { timeout: 15000 }).then(el => el.click());
    await testdriver.find("Connect a Platform heading", { timeout: 15000 });
    const assertMore = await testdriver.assert("the Connect a Platform header is visible at the top of the page");
    expect(assertMore).toBeTruthy();
  });
});
