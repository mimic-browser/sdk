import { launch } from "mimic-browser/playwright";

const session = await launch();
try {
  const context = await session.newContext();
  const page = await context.newPage();
  await page.goto("https://example.com");
  console.log(await page.title());
  console.log(await session.mimic.getVersion());
} finally {
  await session.close();
}
