import puppeteer, {
  type Browser,
  type BrowserContext,
  type Page,
  type ConnectOptions,
  type BrowserContextOptions,
} from "puppeteer-core";
import {
  IntegrationSession,
  launchIntegration,
  type Adapter,
  type LaunchOptions as IntegrationLaunchOptions,
  type ContextSettings as IntegrationContextSettings,
} from "./integration.js";

export type FrameworkOptions = Omit<
  ConnectOptions,
  "browserURL" | "browserWSEndpoint" | "transport"
>;
export type LaunchOptions = IntegrationLaunchOptions<FrameworkOptions>;
export type ContextSettings = IntegrationContextSettings<BrowserContextOptions>;
const adapter: Adapter<
  Browser,
  BrowserContext,
  Page,
  BrowserContextOptions,
  FrameworkOptions
> = {
  validateOptions(options) {
    if (
      ["browserURL", "browserWSEndpoint", "transport"].some((key) =>
        Object.hasOwn(options, key),
      )
    ) {
      throw new Error(
        "The SDK owns the Puppeteer endpoint and transport; pass the endpoint to connect()",
      );
    }
  },
  attach: (endpoint, options) =>
    puppeteer.connect({
      defaultViewport: null,
      ...options,
      ...(/^wss?:/.test(endpoint)
        ? { browserWSEndpoint: endpoint }
        : { browserURL: endpoint }),
    }),
  disconnect: (browser) => browser.disconnect(),
  newContext: (browser, options) => browser.createBrowserContext(options),
  managedOptions(options, attachmentOptions) {
    if (attachmentOptions.defaultViewport != null)
      throw new Error(
        "Managed profile requires Puppeteer defaultViewport:null",
      );
    if (
      Object.hasOwn(options, "proxyServer") ||
      Object.hasOwn(options, "proxyBypassList")
    )
      throw new Error(
        "Managed profile owns proxy settings; use the Mimic proxy option",
      );
    return options;
  },
  closeContext: (context) => context.close(),
  newPage: (context) => context.newPage(),
  closePage: (page) => page.close(),
  isPageClosed: (page) => page.isClosed(),
  onPageClose(page, callback) {
    page.on("close", callback);
    return () => {
      page.off("close", callback);
    };
  },
  async targetInfo(page) {
    const session = await page.createCDPSession();
    try {
      return (await session.send("Target.getTargetInfo")).targetInfo as {
        targetId: string;
        browserContextId: string;
      };
    } finally {
      await session.detach();
    }
  },
};
export function launch(options: LaunchOptions = {}) {
  return launchIntegration(adapter, options);
}
export function connect(
  endpoint: string,
  options: {
    framework?: FrameworkOptions;
    timeout?: number;
    signal?: AbortSignal;
  } = {},
) {
  return IntegrationSession.connect(adapter, endpoint, options);
}
export { IntegrationSession } from "./integration.js";
export type {
  ContextSetup,
  MediaFactory,
  PageExtensions,
} from "./integration.js";
