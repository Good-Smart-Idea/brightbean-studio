import { describe, expect, it } from "vitest";
import { TestDriver } from "testdriverai/vitest/hooks";

describe("BrightBean Studio Signup and Authentication Flow", () => {
  it("should display the login page, navigate to signup, and create an account", async (context) => {
    const testdriver = TestDriver(context);

    // 1. Navigate to the login page
    await testdriver.provision.chrome({
      url: "https://deals-bomb-provide-woods.trycloudflare.com/accounts/login/",
    });

    // 2. Assert login page and sign up link are present
    const loginAssert = await testdriver.assert(
      "The BrightBean Studio login page is visible with a Sign up link"
    );
    expect(loginAssert).toBeTruthy();

    // 3. Click the Sign up link to test the new signup routing
    const signupLink = await testdriver.find("Sign up link");
    await signupLink.click();

    // 4. Wait for and assert arrival at the signup page
    await testdriver.find("Create your account heading", { timeout: 15000 });
    const signupPageAssert = await testdriver.assert(
      "The Create your account heading and Create Account button are visible"
    );
    expect(signupPageAssert).toBeTruthy();

    // 5. Fill out the signup form with a new user
    const emailInput = await testdriver.find("Email address input field");
    await emailInput.click();
    const uniqueEmail = `testuser_${Date.now()}@example.com`;
    await testdriver.type(uniqueEmail);

    const passwordInput = await testdriver.find("Password input field");
    await passwordInput.click();
    await testdriver.type("SecurePassword123!");

    // 6. Submit the form
    const createButton = await testdriver.find("Create Account button");
    await createButton.click();

    // 7. Wait for and verify arrival on the workspace dashboard
    await testdriver.find("Publish navigation link or My Workspace header", { timeout: 25000 });
    const dashboardAssert = await testdriver.assert(
      "The workspace dashboard with navigation links Publish, Create Idea, Social Inbox is visible"
    );
    expect(dashboardAssert).toBeTruthy();
  });
});
