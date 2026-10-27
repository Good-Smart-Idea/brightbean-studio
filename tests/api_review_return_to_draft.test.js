import { describe, expect, it } from "vitest";
import { TestDriver } from "testdriverai/vitest/hooks";

describe("BrightBean Agent API Review Withdrawal Flow", () => {
  it("should verify the API documentation and test return-to-draft endpoint", async (context) => {
    const testdriver = TestDriver(context);

    const baseUrl = process.env.APP_URL || "https://negotiations-fleet-estimates-antarctica.trycloudflare.com";
    await testdriver.provision.chrome({
      url: `${baseUrl}/api/v1/docs`,
    });

    const docAssert = await testdriver.assert("The Brightbean Agent API documentation page is visible");
    expect(docAssert).toBeTruthy();

    const openApiOutput = await testdriver.exec("sh", `curl -s ${baseUrl}/api/v1/openapi.json | grep return-to-draft`, 10000);
    expect(openApiOutput).toBeTruthy();
  });
});
