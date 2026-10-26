import { describe, expect, it } from "vitest";
import { TestDriver } from "testdriverai/vitest/hooks";

describe("Video Preview and Draft Submission Flow", () => {
  it("should log in, open composer, preview on YouTube channel, and save draft", async (context) => {
    const testdriver = TestDriver(context);

    const baseUrl = process.env.APP_URL || "https://suit-route-alpha-blog.trycloudflare.com";
    await testdriver.provision.chrome({
      url: `${baseUrl}/accounts/login/`,
    });

    const loginAssert = await testdriver.assert(
      "The BrightBean Studio login page is visible with email and password fields"
    );
    expect(loginAssert).toBeTruthy();

    await testdriver.find("Email address input field").click();
    await testdriver.type("testuser@example.com");

    await testdriver.find("Password input field").click();
    await testdriver.type("TestPass123!");

    await testdriver.find("Log in button").click();

    await testdriver.find("Publish navigation link or My Workspace header", { timeout: 30000 });

    const newBtn = await testdriver.find("+ New button in the left sidebar", { timeout: 15000 });
    await newBtn.click();

    const postOpt = await testdriver.find("Post option in dropdown menu with description Publish content to a channel", { timeout: 15000 });
    await postOpt.click();

    await testdriver.find("What would you like to share textarea", { timeout: 25000 });

    const composerAssert = await testdriver.assert(
      "The composer is visible with caption input, media upload, and preview pane"
    );
    expect(composerAssert).toBeTruthy();

    const ytBtn = await testdriver.find("Youtube Channel button in composer", { timeout: 15000 });
    await ytBtn.click();

    const previewAssert = await testdriver.assert(
      "The YouTube preview card is visible in the preview pane"
    );
    expect(previewAssert).toBeTruthy();

    await testdriver.find("What would you like to share textarea").click();
    await testdriver.type("Test video preview and draft submission");

    await testdriver.find("Save Draft button").click();

    await testdriver.find("List view tab button in the header", { timeout: 25000 });

    const calendarAssert = await testdriver.assert(
      "The Publish calendar dashboard is visible with List and Calendar view buttons"
    );
    expect(calendarAssert).toBeTruthy();

    const listTab = await testdriver.find("List view tab button in the header", { timeout: 15000 });
    await listTab.click();

    const draftsTab = await testdriver.find("Drafts sub-tab in the List view", { timeout: 15000 });
    await draftsTab.click();

    await testdriver.find("Drafts tab showing 1 or more draft posts", { timeout: 25000 });

    const draftAssert = await testdriver.assert(
      "The draft post with caption Test video preview and draft submission is visible in the Drafts list"
    );
    expect(draftAssert).toBeTruthy();
  });
});
