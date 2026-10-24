import { describe, expect, it } from "vitest";
import { TestDriver } from "testdriverai/vitest/hooks";

describe("AI Providers Settings and Test Connection", () => {
  it("should navigate to AI Providers settings, configure provider, and verify test connection feedback", async (context) => {
    const testdriver = TestDriver(context);

    // 1. Navigate to login
    await testdriver.provision.chrome({ url: "https://log-champagne-penalties-joshua.trycloudflare.com/accounts/login/" });

    // 2. Log in
    const emailInput = await testdriver.find("Email address input field");
    await emailInput.click();
    await testdriver.type("testdriver@example.com");

    const passwordInput = await testdriver.find("Password input field");
    await passwordInput.click();
    await testdriver.type("TestDriverPass123!");

    const loginButton = await testdriver.find("Log In button");
    await loginButton.click();

    // 3. Navigate to AI Providers settings
    await testdriver.pressKeys(["ctrl", "l"]);
    await testdriver.type("https://log-champagne-penalties-joshua.trycloudflare.com/organizations/ai-providers/");
    await testdriver.pressKeys(["enter"]);

    // 4. Assert AI Providers page is loaded
    const headingAssert = await testdriver.assert("The AI Providers heading and OpenAI card are visible");
    expect(headingAssert).toBeTruthy();

    // 5. Configure OpenAI provider
    const apiKeyInput = await testdriver.find("API Key input field for OpenAI");
    await apiKeyInput.click();
    await testdriver.type("sk-test-key-for-ui-verification-1234");

    const modelInput = await testdriver.find("Default Model input field for OpenAI");
    await modelInput.click();
    await testdriver.type("gpt-4o-mini");

    const saveButton = await testdriver.find("Save button in OpenAI card");
    await saveButton.click();

    // 6. Assert key is masked and Test connection button appears
    await testdriver.find("Current key ****1234", { timeout: 15000 });
    const maskedAssert = await testdriver.assert("The current key is masked showing only the last four digits and the Test connection button is visible");
    expect(maskedAssert).toBeTruthy();

    // 7. Click Test connection
    const testButton = await testdriver.find("Test connection button in OpenAI card");
    await testButton.click();

    // 8. Wait for connection test result and verify the friendly error message
    await testdriver.find("OpenAI test failed", { timeout: 30000 });
    const errorAssert = await testdriver.assert("The OpenAI test connection failure shows the friendly message that the API key was rejected by the provider and does not expose the key");
    expect(errorAssert).toBeTruthy();
  });
});
