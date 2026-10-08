import puppeteer, {
  type Browser,
  type BrowserContext,
  type Page,
  type ConnectOptions,
} from "puppeteer-core";
import {
  IntegrationSession,
  launchIntegration,
  type Adapter,
  type LaunchOptions,
} from "./integration.js";

const adapter: Adapter<Browser, BrowserContext, Page> = {
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
    framework?: ConnectOptions;
    timeout?: number;
    signal?: AbortSignal;
  } = {},
) {
  return IntegrationSession.connect(adapter, endpoint, options as any);
}
export { IntegrationSession } from "./integration.js";
export type {
  ContextSetup,
  ContextSettings,
  MediaFactory,
} from "./integration.js";
