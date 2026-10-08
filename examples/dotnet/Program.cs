using Mimic.Playwright;
using Mimic.Sdk;

await using var session = await PlaywrightSession.LaunchAsync(new RuntimeOptions
{
    ExecutablePath = args.FirstOrDefault(),
    AllowDownload = args.Length == 0
});
var context = await session.NewContextAsync();
var page = await context.NewPageAsync();
await page.SetContentAsync("<h1>Native Playwright on Mimic</h1>");
Console.WriteLine(await page.Locator("h1").TextContentAsync());
Console.WriteLine((await session.Mimic.Commands.GetVersionAsync()).Version);
