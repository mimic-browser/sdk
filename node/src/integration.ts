import {
  MimicClient,
  type CreateContextParams,
  type MediaConfiguration,
  type GetVersionResult,
} from "./generated.js";
import {
  CDPConnection,
  ProtocolError,
  experimental,
  type Sender,
} from "./protocol.js";
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

/** A Page-bound extension handle; closing it never closes the native Page. */
export type PageExtensions = ReturnType<typeof extensions> & {
  readonly closed: boolean;
  close(): Promise<void>;
  detach(): Promise<void>;
  [Symbol.asyncDispose](): Promise<void>;
};

export interface ContextSetup {
  browserContextId: string;
  mimic: ReturnType<typeof extensions>;
}
export type MediaFactory = (
  setup: ContextSetup,
) => MediaConfiguration | Promise<MediaConfiguration>;
export interface ContextSettings<
  Options extends object = Record<string, unknown>,
> {
  media?: MediaConfiguration | MediaFactory;
  resourcePolicy?: CreateContextParams["resourcePolicy"];
  profile?: CreateContextParams["profile"];
  proxy?: CreateContextParams["proxy"];
  framework?: Options;
}
export interface Adapter<
  B,
  C,
  P,
  ContextOptions extends object,
  FrameworkOptions extends object,
> {
  validateOptions?(options: FrameworkOptions): void;
  attach(endpoint: string, options: FrameworkOptions): Promise<B>;
  disconnect(browser: B): Promise<void>;
  newContext(browser: B, options: ContextOptions): Promise<C>;
  managedOptions?(
    options: ContextOptions,
    attachmentOptions: FrameworkOptions,
  ): ContextOptions;
  closeContext(context: C): Promise<void>;
  newPage(context: C): Promise<P>;
  closePage(page: P): Promise<void>;
  targetInfo(page: P): Promise<{ targetId: string; browserContextId: string }>;
  isPageClosed(page: P): boolean;
  onPageClose(page: P, callback: () => void): () => void;
}

export class IntegrationSession<
  B,
  C,
  P,
  ContextOptions extends object = Record<string, unknown>,
  FrameworkOptions extends object = Record<string, unknown>,
> {
  readonly mimic;
  readonly identity: GetVersionResult;
  private contexts = new Set<C>();
  private inflight = new Set<Promise<unknown>>();
  private pageHandles = new Map<P, Promise<PageExtensions>>();
  private attachments = new Set<PageExtensions>();
  private closed = false;
  private closePromise?: Promise<void>;
  closeErrors: unknown[] = [];
  private constructor(
    readonly browser: B,
    readonly connection: CDPConnection,
    private adapter: Adapter<B, C, P, ContextOptions, FrameworkOptions>,
    readonly runtime: RuntimeProcess | undefined,
    identity: GetVersionResult,
    private attachmentOptions: FrameworkOptions,
  ) {
    this.identity = identity;
    this.mimic = extensions(connection.call.bind(connection));
  }
  static async connect<
    B,
    C,
    P,
    ContextOptions extends object,
    FrameworkOptions extends object,
  >(
    adapter: Adapter<B, C, P, ContextOptions, FrameworkOptions>,
    endpoint: string,
    options: {
      runtime?: RuntimeProcess;
      framework?: FrameworkOptions;
      timeout?: number;
      signal?: AbortSignal;
    } = {},
  ) {
    let connection: CDPConnection | undefined;
    let browser: B | undefined;
    try {
      adapter.validateOptions?.(options.framework || ({} as FrameworkOptions));
      connection =
        options.runtime?.connection ||
        (await CDPConnection.connect(endpoint, options));
      const identity = await connection.call("Mimic.getVersion", undefined, {
        signal: options.signal,
      });
      if (typeof identity?.version !== "string")
        throw new Error("Endpoint did not identify itself as Mimic");
      browser = await adapter.attach(
        endpoint,
        options.framework || ({} as FrameworkOptions),
      );
      options.signal?.throwIfAborted();
      return new IntegrationSession(
        browser,
        connection,
        adapter,
        options.runtime,
        identity,
        options.framework || ({} as FrameworkOptions),
      );
    } catch (error) {
      if (browser) await adapter.disconnect(browser).catch(() => {});
      if (options.runtime) await options.runtime.close();
      else connection?.close();
      throw error;
    }
  }

  newContext(settings: ContextSettings<ContextOptions> = {}): Promise<C> {
    return this.createContext(settings);
  }

  private async createContext({
    media,
    resourcePolicy,
    profile,
    proxy,
    framework = {} as ContextOptions,
  }: ContextSettings<ContextOptions> = {}): Promise<C> {
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

  forPage(page: P): Promise<PageExtensions> {
    if (this.closed)
      return Promise.reject(new Error("Integration session closed"));
    const existing = this.pageHandles.get(page);
    if (existing) return existing;
    const operation = this.attachPage(page);
    this.pageHandles.set(page, operation);
    this.inflight.add(operation);
    operation
      .finally(() => this.inflight.delete(operation))
      .catch(() => {
        if (this.pageHandles.get(page) === operation)
          this.pageHandles.delete(page);
      });
    return operation;
  }

  private async attachPage(page: P): Promise<PageExtensions> {
    let closed = this.adapter.isPageClosed(page);
    let ownedPage: P | undefined = page;
    let handle: PageExtensions | undefined;
    let detachOwned: (() => Promise<void>) | undefined;
    let unsubscribe = () => {};
    const controller = new AbortController();
    const release = () => {
      closed = true;
      controller.abort(new Error("Page extension handle closed"));
      const removeListener = unsubscribe;
      unsubscribe = () => {};
      removeListener();
      if (ownedPage !== undefined) this.pageHandles.delete(ownedPage);
      ownedPage = undefined;
      if (handle) this.attachments.delete(handle);
    };
    // Register before the first await so a close during target discovery or
    // attachment cannot leave an owned CDP session behind.
    unsubscribe = this.adapter.onPageClose(page, () => {
      release();
      if (detachOwned) void detachOwned();
    });
    try {
      if (closed) throw new Error("Page closed");
      const { targetId } = await this.adapter.targetInfo(page);
      if (closed || this.closed)
        throw new Error("Page or integration session closed");
      const { sessionId } = await this.connection.call(
        "Target.attachToTarget",
        {
          targetId,
          flatten: true,
        },
      );
      let closePromise: Promise<void> | undefined;
      const detach = () => {
        if (closePromise) return closePromise;
        release();
        closePromise = this.connection
          .call("Target.detachFromTarget", { sessionId })
          .then(
            () => {},
            (error) => {
              // Page destruction already removes its target sessions on the wire.
              if (
                !(
                  error instanceof ProtocolError &&
                  (error.code === -32001 ||
                    (error.code === -32000 &&
                      error.message === "No session with given id"))
                )
              )
                throw error;
            },
          );
        this.inflight.add(closePromise);
        const cleanup = closePromise;
        cleanup
          .catch((error) => this.closeErrors.push(error))
          .finally(() => this.inflight.delete(cleanup));
        return closePromise;
      };
      detachOwned = detach;
      handle = Object.defineProperties(
        extensions((method, params) => {
          if (closed)
            return Promise.reject(new Error("Page extension handle closed"));
          return this.connection.call(method, params, {
            sessionId,
            signal: controller.signal,
          });
        }),
        {
          closed: { get: () => closed },
          close: { value: detach },
          detach: { value: detach },
          [Symbol.asyncDispose]: { value: () => handle!.close() },
        },
      ) as PageExtensions;
      if (closed || this.closed) {
        await detach();
        throw new Error("Page or integration session closed");
      }
      this.attachments.add(handle);
      return handle;
    } catch (error) {
      release();
      throw error;
    }
  }

  async close() {
    if (this.closePromise) return this.closePromise;
    this.closed = true;
    this.closePromise = this.finishClose();
    return this.closePromise;
  }
  private async finishClose() {
    await Promise.allSettled(this.inflight);
    for (const handle of this.attachments) {
      try {
        await handle.close();
      } catch {
        /* The owned detach promise already records its failure. */
      }
    }
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

export interface LaunchOptions<
  FrameworkOptions extends object = Record<string, unknown>,
> extends RuntimeOptions {
  engine?: "v8" | "quickjs" | "goja";
  signal?: AbortSignal;
  framework?: FrameworkOptions;
}
export async function launchIntegration<
  B,
  C,
  P,
  ContextOptions extends object,
  FrameworkOptions extends object,
>(
  adapter: Adapter<B, C, P, ContextOptions, FrameworkOptions>,
  options: LaunchOptions<FrameworkOptions> = {},
) {
  adapter.validateOptions?.(options.framework || ({} as FrameworkOptions));
  const runtime = await new RuntimeManager(options).launch(options);
  return IntegrationSession.connect(adapter, runtime.endpoint, {
    ...options,
    runtime,
  });
}
