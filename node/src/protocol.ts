/** One extension connection and dispatcher for stable and experimental CDP calls. */
export const OMITTED = Symbol("omitted CDP params");

export class ProtocolError extends Error {
  constructor(
    public readonly code: number,
    message: string,
    public readonly data: unknown = OMITTED,
  ) {
    super(message);
    this.name = "ProtocolError";
  }
}

export function assertJSON(value: unknown, seen = new Set<unknown>()): void {
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (typeof value !== "object" || value === undefined || seen.has(value)) {
    throw new TypeError(
      "Expected JSON values without cycles, undefined, BigInt or non-finite numbers",
    );
  }
  if (
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) !== Object.prototype &&
    Object.getPrototypeOf(value) !== null
  ) {
    throw new TypeError("Expected a plain JSON object");
  }
  seen.add(value);
  if (Object.getOwnPropertySymbols(value).length)
    throw new TypeError("JSON cannot represent symbol keys");
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) assertJSON(value[i], seen);
  } else {
    for (const item of Object.values(value)) assertJSON(item, seen);
  }
  seen.delete(value);
}

export async function websocketEndpoint(
  endpoint: string,
  signal?: AbortSignal,
): Promise<string> {
  const url = new URL(endpoint);
  if (url.protocol === "ws:" || url.protocol === "wss:") return endpoint;
  if (url.protocol !== "http:" && url.protocol !== "https:")
    throw new Error("Expected HTTP(S) or WS(S) endpoint");
  const response = await fetch(endpoint.replace(/\/$/, "") + "/json/version", {
    signal,
  });
  if (!response.ok)
    throw new Error(`CDP discovery returned HTTP ${response.status}`);
  const result = (await response.json()) as { webSocketDebuggerUrl?: string };
  if (
    !result.webSocketDebuggerUrl ||
    !/^wss?:$/.test(new URL(result.webSocketDebuggerUrl).protocol)
  ) {
    throw new Error("Discovery omitted the browser websocket endpoint");
  }
  return result.webSocketDebuggerUrl;
}

type Pending = {
  sessionId?: string;
  resolve(value: any): void;
  reject(error: unknown): void;
  cleanup(): void;
};
export type Sender = (method: string, params?: unknown) => Promise<any>;

export class CDPConnection {
  private sequence = 0;
  private pending = new Map<number, Pending>();
  private closed = false;
  private constructor(
    private socket: WebSocket,
    public readonly timeout: number,
  ) {
    socket.addEventListener("message", ({ data }) => {
      try {
        const message = JSON.parse(String(data));
        const pending = this.pending.get(message.id);
        if (!pending) return;
        if (message.sessionId !== pending.sessionId) return;
        this.pending.delete(message.id);
        pending.cleanup();
        if (message.error)
          pending.reject(
            new ProtocolError(
              message.error.code,
              message.error.message,
              Object.hasOwn(message.error, "data")
                ? message.error.data
                : OMITTED,
            ),
          );
        else pending.resolve(message.result);
      } catch (error) {
        this.rejectAll(error);
        this.socket.close();
      }
    });
    socket.addEventListener("close", () =>
      this.rejectAll(new Error("CDP connection closed")),
    );
    socket.addEventListener("error", () =>
      this.rejectAll(new Error("CDP connection failed")),
    );
  }

  static async connect(
    endpoint: string,
    {
      timeout = 30_000,
      signal,
    }: { timeout?: number; signal?: AbortSignal } = {},
  ) {
    const bounded = signal
      ? AbortSignal.any([signal, AbortSignal.timeout(timeout)])
      : AbortSignal.timeout(timeout);
    const socket = new WebSocket(await websocketEndpoint(endpoint, bounded));
    await new Promise<void>((resolve, reject) => {
      const cleanup = () => {
        socket.removeEventListener("open", opened);
        socket.removeEventListener("error", failed);
        bounded.removeEventListener("abort", aborted);
      };
      const opened = () => {
        cleanup();
        resolve();
      };
      const failed = () => {
        cleanup();
        reject(new Error("Could not connect to CDP endpoint"));
      };
      const aborted = () => {
        cleanup();
        socket.close();
        reject(bounded.reason);
      };
      socket.addEventListener("open", opened);
      socket.addEventListener("error", failed);
      bounded.addEventListener("abort", aborted, { once: true });
      if (bounded.aborted) aborted();
    });
    return new CDPConnection(socket, timeout);
  }

  call(
    method: string,
    params: unknown = OMITTED,
    {
      sessionId,
      signal,
      timeout = this.timeout,
    }: {
      sessionId?: string;
      signal?: AbortSignal;
      timeout?: number;
    } = {},
  ): Promise<any> {
    if (this.closed) return Promise.reject(new Error("CDP connection closed"));
    if (!method || /[\x00-\x1f]/.test(method))
      return Promise.reject(new TypeError("Expected exact CDP method"));
    if (params !== OMITTED) assertJSON(params);
    signal?.throwIfAborted();
    const id = ++this.sequence;
    const message: Record<string, unknown> = { id, method };
    if (params !== OMITTED) message.params = params;
    if (sessionId !== undefined) message.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const fail = (error: unknown) => {
        this.pending.delete(id);
        cleanup();
        reject(error);
      };
      const timer = setTimeout(
        () =>
          fail(
            new Error(
              `${method} timed out; dispatch may already have completed`,
            ),
          ),
        timeout,
      );
      const aborted = () => fail(signal!.reason);
      const cleanup = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", aborted);
      };
      signal?.addEventListener("abort", aborted, { once: true });
      this.pending.set(id, { resolve, reject, cleanup, sessionId });
      try {
        this.socket.send(JSON.stringify(message));
      } catch (error) {
        fail(error);
      }
    });
  }

  private rejectAll(error: unknown) {
    this.closed = true;
    for (const pending of this.pending.values()) {
      pending.cleanup();
      pending.reject(error);
    }
    this.pending.clear();
  }

  close() {
    this.rejectAll(new Error("CDP connection closed"));
    this.socket.close();
  }
}

export interface Experimental {
  call(name: string, params?: unknown): Promise<any>;
  [name: string]: any;
}
export function experimental(sender: Sender): Experimental {
  const call = (name: string, params: unknown = OMITTED) => {
    if (typeof name !== "string" || !name || /[.\x00-\x1f]/.test(name))
      throw new TypeError("Expected exact Mimic command leaf");
    return sender(`Mimic.${name}`, params);
  };
  const reserved = new Set([
    "then",
    "toJSON",
    "inspect",
    "constructor",
    "toString",
    "valueOf",
  ]);
  return new Proxy({ call } as Experimental, {
    get(target, key) {
      if (typeof key === "symbol" || reserved.has(key)) return undefined;
      if (key === "call") return target.call;
      return (params: unknown = OMITTED) => call(key, params);
    },
  });
}
