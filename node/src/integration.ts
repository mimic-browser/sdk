import {
  MimicClient,
  type CreateContextParams,
  type MediaConfiguration,
} from "./generated.js";
import { CDPConnection, experimental, type Sender } from "./protocol.js";
import {
  RuntimeManager,
  type RuntimeOptions,
  type RuntimeProcess,
} from "./runtime.js";

export function extensions(sender: Sender) {
  return Object.assign(new MimicClient(sender), {
    experimental: experimental(sender),
  });
}

export interface ContextSetup {
  browserContextId: string;
  mimic: ReturnType<typeof extensions>;
}
export type MediaFactory = (
  setup: ContextSetup,
) => MediaConfiguration | Promise<MediaConfiguration>;
export interface ContextSettings {
  media?: MediaConfiguration | MediaFactory;
  resourcePolicy?: CreateContextParams["resourcePolicy"];
  profile?: CreateContextParams["profile"];
  proxy?: CreateContextParams["proxy"];
  framework?: Record<string, unknown>;
}
export interface Adapter<B, C, P> {
  attach(endpoint: string, options: Record<string, unknown>): Promise<B>;
  disconnect(browser: B): Promise<void>;
  newContext(browser: B, options: Record<string, unknown>): Promise<C>;
  managedOptions?(
    options: Record<string, unknown>,
    attachmentOptions: Record<string, unknown>,
  ): Record<string, unknown>;
  closeContext(context: C): Promise<void>;
  newPage(context: C): Promise<P>;
  closePage(page: P): Promise<void>;
  targetInfo(page: P): Promise<{ targetId: string; browserContextId: string }>;
}

export class IntegrationSession<B, C, P> {
  readonly mimic;
  readonly identity: any;
  private contexts = new Set<C>();
  private inflight = new Set<Promise<unknown>>();
  private closed = false;
  private closePromise?: Promise<void>;
  closeErrors: unknown[] = [];
  private constructor(
    readonly browser: B,
    readonly connection: CDPConnection,
    private adapter: Adapter<B, C, P>,
    readonly runtime: RuntimeProcess | undefined,
    identity: any,
    private attachmentOptions: Record<string, unknown>,
  ) {
    this.identity = identity;
    this.mimic = extensions(connection.call.bind(connection));
  }
  static async connect<B, C, P>(
    adapter: Adapter<B, C, P>,
    endpoint: string,
    options: {
      runtime?: RuntimeProcess;
      framework?: Record<string, unknown>;
      timeout?: number;
      signal?: AbortSignal;
    } = {},
  ) {
    let connection: CDPConnection | undefined;
    let browser: B | undefined;
    try {
      connection =
        options.runtime?.connection ||
        (await CDPConnection.connect(endpoint, options));
      const identity = await connection.call("Mimic.getVersion", undefined, {
        signal: options.signal,
      });
      if (typeof identity?.version !== "string")
        throw new Error("Endpoint did not identify itself as Mimic");
      browser = await adapter.attach(endpoint, options.framework || {});
      options.signal?.throwIfAborted();
      return new IntegrationSession(
        browser,
        connection,
        adapter,
        options.runtime,
        identity,
        options.framework || {},
      );
    } catch (error) {
      if (browser) await adapter.disconnect(browser).catch(() => {});
      if (options.runtime) await options.runtime.close();
      else connection?.close();
      throw error;
    }
  }

  newContext(settings: ContextSettings = {}): Promise<C> {
    return this.createContext(settings);
  }

  private async createContext({
    media,
    resourcePolicy,
    profile,
    proxy,
    framework = {},
  }: ContextSettings = {}): Promise<C> {
    if (this.closed) throw new Error("Integration session closed");
    const managed = profile !== undefined || proxy !== undefined;
    const options =
      managed && this.adapter.managedOptions
        ? this.adapter.managedOptions(framework, this.attachmentOptions)
        : framework;
    // Close waits for native acquisition so a Context cannot escape ownership.
    // User factories are outside that barrier: a factory may itself await close.
    const acquisition = this.adapter
      .newContext(this.browser, options)
      .then((context) => {
        this.contexts.add(context);
        return context;
      });
    this.inflight.add(acquisition);
    acquisition
      .finally(() => this.inflight.delete(acquisition))
      .catch(() => {});
    const context = await acquisition;
    try {
      if (this.closed) throw new Error("Integration session closed");
      if (managed || media !== undefined || resourcePolicy !== undefined) {
        const probe = await this.adapter.newPage(context);
        let info;
        try {
          info = await this.adapter.targetInfo(probe);
        } finally {
          await this.adapter.closePage(probe);
        }
        if (typeof media === "function") {
          media = await media({
            browserContextId: info.browserContextId,
            mimic: this.mimic,
          });
          if (this.closed) throw new Error("Integration session closed");
          if (!media || typeof media !== "object")
            throw new TypeError(
              "Media factory must return a media configuration",
            );
        }
        if (managed) {
          await this.connection.call("Mimic.configureContext", {
            browserContextId: info.browserContextId,
            profile: profile === undefined ? { generate: {} } : profile,
            ...(proxy === undefined ? {} : { proxy }),
            ...(media === undefined ? {} : { media }),
            ...(resourcePolicy === undefined ? {} : { resourcePolicy }),
          });
        } else if (media !== undefined)
          await this.connection.call("Mimic.setMediaProfile", {
            ...media,
            browserContextId: info.browserContextId,
          });
        if (!managed && resourcePolicy !== undefined)
          await this.connection.call("Mimic.updateResourcePolicy", {
            policy: resourcePolicy,
            browserContextId: info.browserContextId,
          });
      }
      if (this.closed) throw new Error("Integration session closed");
      return context;
    } catch (error) {
      // Once closing starts, finishClose owns cleanup of all acquired Contexts.
      // Preserve the factory/configuration error even if disposal also fails.
      if (!this.closed) {
        try {
          await this.adapter.closeContext(context);
          this.contexts.delete(context);
        } catch (cleanupError) {
          this.closeErrors.push(cleanupError);
        }
      }
      throw error;
    }
  }

  async forPage(page: P) {
    const { targetId } = await this.adapter.targetInfo(page);
    const { sessionId } = await this.connection.call("Target.attachToTarget", {
      targetId,
      flatten: true,
    });
    return extensions((method, params) =>
      this.connection.call(method, params, { sessionId }),
    );
  }

  async close() {
    if (this.closePromise) return this.closePromise;
    this.closed = true;
    this.closePromise = this.finishClose();
    return this.closePromise;
  }
  private async finishClose() {
    await Promise.allSettled(this.inflight);
    for (const context of this.contexts) {
      try {
        await this.adapter.closeContext(context);
      } catch (error) {
        this.closeErrors.push(error);
      }
    }
    try {
      await this.adapter.disconnect(this.browser);
    } catch (error) {
      this.closeErrors.push(error);
    }
    if (this.runtime) await this.runtime.close();
    else this.connection.close();
  }
  async [Symbol.asyncDispose]() {
    await this.close();
  }
}

export interface LaunchOptions extends RuntimeOptions {
  engine?: "v8" | "quickjs" | "goja";
  signal?: AbortSignal;
  framework?: Record<string, unknown>;
}
export async function launchIntegration<B, C, P>(
  adapter: Adapter<B, C, P>,
  options: LaunchOptions = {},
) {
  const runtime = await new RuntimeManager(options).launch(options);
  return IntegrationSession.connect(adapter, runtime.endpoint, {
    ...options,
    runtime,
  });
}
