import {
  chromium,
  type Browser,
  type BrowserContext,
  type Page,
  type ConnectOverCDPOptions,
} from "playwright-core";
import {
  IntegrationSession,
  launchIntegration,
  type Adapter,
  type LaunchOptions,
} from "./integration.js";

const adapter: Adapter<Browser, BrowserContext, Page> = {
  attach: (endpoint, options) =>
    chromium.connectOverCDP(endpoint, options as ConnectOverCDPOptions),
  disconnect: (browser) => browser.close(),
  newContext: (browser, options) => browser.newContext(options),
  managedOptions(options) {
    const conflicting = [
      "viewport",
      "screen",
      "deviceScaleFactor",
      "isMobile",
      "hasTouch",
      "userAgent",
      "locale",
      "timezoneId",
      "colorScheme",
      "reducedMotion",
      "forcedColors",
      "contrast",
      "proxy",
    ];
    if (conflicting.some((key) => Object.hasOwn(options, key)))
      throw new Error(
        "Managed profile owns emulation; configure it in the profile",
      );
    return {
      ...options,
      viewport: null,
      colorScheme: null,
      reducedMotion: null,
      forcedColors: null,
      contrast: null,
    };
  },
  closeContext: (context) => context.close(),
  newPage: (context) => context.newPage(),
  closePage: (page) => page.close(),
  async targetInfo(page) {
    const session = await page.context().newCDPSession(page);
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
    framework?: ConnectOverCDPOptions;
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
