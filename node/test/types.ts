import {
  launch,
  connect,
  type ContextSettings,
} from "mimic-browser/playwright";
import { launch as launchPuppeteer } from "mimic-browser/puppeteer";
import type { GetVersionResult } from "mimic-browser";

async function typedClients() {
  const settings: ContextSettings = {
    framework: { locale: "en-US", viewport: { width: 800, height: 600 } },
  };
  const session = await launch({
    runtimeVersion: "0.2.3",
    framework: { timeout: 1000 },
  });
  const context = await session.newContext(settings);
  const page = await context.newPage();
  const extension = await session.forPage(page);
  const version: GetVersionResult = await session.mimic.getVersion();
  const closed: boolean = extension.closed;
  await extension.detach();
  // @ts-expect-error unknown Mimic launch option
  await launch({ runtime_version: "0.2.3" });
  // @ts-expect-error native Playwright options are checked
  await session.newContext({ framework: { localle: "en-US" } });
  // @ts-expect-error native Playwright option value is checked
  await session.newContext({ framework: { viewport: "wide" } });
  // @ts-expect-error extension results are generated models, not any
  const number: number = version.version;
  // @ts-expect-error native connection options are checked
  await connect("http://localhost:9222", { framework: { timeuot: 5 } });
  const puppeteer = await launchPuppeteer({
    framework: { defaultViewport: null },
  });
  // @ts-expect-error the SDK owns the connection endpoint
  await launchPuppeteer({ framework: { browserURL: "http://localhost:9222" } });
  await puppeteer.newContext({
    framework: { downloadBehavior: { policy: "deny" } },
  });
  // @ts-expect-error Puppeteer context options retain their own native shape
  await puppeteer.newContext({ framework: { viewport: null } });
  return { closed, number };
}
void typedClients;
