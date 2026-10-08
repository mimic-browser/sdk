import { launch } from "../../node/dist/puppeteer.js";

const session = await launch({ runtimeVersion: "0.2.2" });
try {
  const context = await session.newContext();
  const page = await context.newPage();
  await page.goto("https://example.com");
  console.log(await page.title());
} finally {
  await session.close();
}
