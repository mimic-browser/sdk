namespace Mimic.Sdk;

/// <summary>Explicit Context identity available before the application's first page.</summary>
public sealed record ContextSetup(string BrowserContextId, MimicClient Mimic);
