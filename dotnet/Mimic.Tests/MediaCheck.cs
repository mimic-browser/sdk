using System.Text.Json.Nodes;
using Mimic.Playwright;
using Mimic.PuppeteerSharp;
using Mimic.Sdk;
using Mimic.Sdk.Generated;

internal static class MediaCheck
{
    private static void Check(bool value, string message) { if (!value) throw new Exception(message); }
    public static async Task RunAsync(string endpoint, string fixture)
    {
        Check(OperatingSystem.IsLinux(), "Synthetic media tests are Linux-only");
        using var http = new HttpClient();
        var script = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "media_capture.js")).TrimEnd().TrimEnd(';');
        foreach (var adapter in new[] { "playwright", "puppeteer" })
        {
            var before = JsonNode.Parse(await http.GetStringAsync(fixture + "/state"))!.AsObject();
            string? contextId = null, privateSource = null;
            async Task<MediaConfiguration> Media(ContextSetup setup, CancellationToken token)
            {
                contextId = setup.BrowserContextId;
                var sources = await setup.Mimic.Commands.GetMediaSourcesAsync(new() { BrowserContextId = Optional<string>.Of(contextId) }, token);
                var camera = sources.Sources.Single(source => source.Label == "Private native camera B");
                var microphone = sources.Sources.Single(source => source.Label == "Private native microphone B");
                privateSource = camera.SourceId;
                Check(!privateSource.Contains("native"), "Private physical ID was exposed as Context source ID");
                return Wire.Decode<MediaConfiguration>(new JsonObject
                {
                    ["devices"] = new JsonArray(
                        new JsonObject { ["key"] = "front", ["kind"] = "videoinput", ["source"] = new JsonObject { ["sourceId"] = camera.SourceId }, ["label"] = "Studio Camera", ["group"] = "desk", ["modes"] = new JsonArray(new JsonObject { ["width"] = 16, ["height"] = 8, ["frameRate"] = 30 }), ["defaultMode"] = new JsonObject { ["width"] = 16, ["height"] = 8, ["frameRate"] = 30 }, ["processing"] = new JsonObject { ["resize"] = "crop-and-scale" } },
                        new JsonObject { ["key"] = "voice", ["kind"] = "audioinput", ["source"] = new JsonObject { ["sourceId"] = microphone.SourceId }, ["label"] = "Studio Microphone", ["group"] = "desk" })
                });
            }
            async Task Grant(MimicClient mimic)
            {
                var after = await mimic.Commands.GetMediaSourcesAsync(new() { BrowserContextId = Optional<string>.Of(contextId!) });
                Check(after.Sources.Any(source => source.SourceId == privateSource), "Profile configuration invalidated Context source ID");
                Check(JsonNode.DeepEquals(before, JsonNode.Parse(await http.GetStringAsync(fixture + "/state"))), "Discovery/configuration opened capture");
                await mimic.SendAsync("Browser.grantPermissions", new() { ["browserContextId"] = contextId, ["origin"] = fixture, ["permissions"] = new JsonArray("videoCapture", "audioCapture") });
            }
            var configuration = JsonNode.Parse("{\"profile\":{\"generate\":{\"seed\":\"dotnet-media-environment\"}}}")!.AsObject();
            JsonObject observed;
            if (adapter == "playwright")
            {
                await using var session = await PlaywrightSession.ConnectAsync(endpoint);
                var context = await session.NewConfiguredContextAsync(configuration, mediaFactory: Media);
                Check(context.Pages.Count == 0, "Media setup leaked probe page");
                await Grant(session.Mimic);
                var page = await context.NewPageAsync(); await page.GotoAsync(fixture); await page.Mouse.ClickAsync(1, 1);
                observed = JsonNode.Parse((await page.EvaluateAsync<System.Text.Json.JsonElement>(script)).GetRawText())!.AsObject();
            }
            else
            {
                await using var session = await PuppeteerSession.ConnectAsync(endpoint);
                var context = await session.NewConfiguredContextAsync(configuration, mediaFactory: Media);
                await Grant(session.Mimic);
                var page = await context.NewPageAsync(); await page.GoToAsync(fixture); await page.Mouse.ClickAsync(1, 1);
                observed = JsonNode.Parse((await page.EvaluateFunctionAsync<System.Text.Json.JsonElement>(script)).GetRawText())!.AsObject();
            }
            Check(observed["pixel"]!.AsArray().Select(value => value!.GetValue<int>()).SequenceEqual(new[] { 0, 0, 255, 255 }), "Private B camera did not deliver blue pixels");
            var audio = observed["audioEnergy"]!;
            Check(audio["b"]!.GetValue<double>() > 1 && audio["b"]!.GetValue<double>() > 5 * audio["a"]!.GetValue<double>(), "Private B microphone PCM did not reach Web Audio");
            Check(!observed.ToJsonString().Contains("Private native") && !observed.ToJsonString().Contains(privateSource!), "Private identity leaked to page");
            var devices = observed["devices"]!.AsArray();
            Check(devices.Count == 2 && devices[0]!["groupId"]!.GetValue<string>() == devices[1]!["groupId"]!.GetValue<string>(), "Public media grouping changed");
            JsonObject state = new();
            for (var attempt = 0; attempt < 100; attempt++)
            {
                state = JsonNode.Parse(await http.GetStringAsync(fixture + "/state"))!.AsObject();
                if (state["opens"]!.AsObject().All(entry => state["closes"]![entry.Key]?.GetValue<int>() == entry.Value!.GetValue<int>())) break;
                await Task.Delay(20);
            }
            foreach (var source in new[] { "native-camera-b", "native-microphone-b" }) Check(state["opens"]![source]!.GetValue<int>() == (before["opens"]![source]?.GetValue<int>() ?? 0) + 1 && state["closes"]![source]!.GetValue<int>() == state["opens"]![source]!.GetValue<int>(), "Private capture worker leaked or wrong source opened");
            Check(JsonNode.DeepEquals(state["opens"]!["native-camera-a"], before["opens"]!["native-camera-a"]), "Capture silently selected source A");
            Console.WriteLine("PASS .NET " + adapter + " public identities, private B blue frames/660 Hz PCM, capture cleanup");
        }
    }
}
