import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import { StringDecoder } from "node:string_decoder";
export type RecordValue = Record<string, unknown>;
export class RpcClient {
  private child: ChildProcessWithoutNullStreams;
  private buffer = "";
  private decoder = new StringDecoder("utf8");
  private serial = 0;
  private ended = false;
  private pending = new Map<
    number,
    { resolve: (value: unknown) => void; reject: (error: Error) => void }
  >();
  constructor(
    python: string,
    cwd: string,
    private callback: (method: string, params: RecordValue) => Promise<unknown>,
    private event: (params: RecordValue) => void,
    private exited: () => void = () => {},
  ) {
    this.child = spawn(python, ["-I", "-m", "agent.server"], {
      cwd,
      stdio: "pipe",
      shell: false,
      windowsHide: true,
    });
    this.child.stdout.on("data", (chunk: Buffer) =>
      this.consume(this.decoder.write(chunk)),
    );
    this.child.stderr.on("data", () => {
      /* Raw provider errors must not reach logs or the webview. */
    });
    this.child.on("error", () => this.fail());
    this.child.on("exit", () => this.fail());
    this.child.stdin.on("error", () => this.fail());
  }
  private fail() {
    if (this.ended) return;
    this.ended = true;
    for (const p of this.pending.values())
      p.reject(Error("Backend disconnected. Run setup or retry."));
    this.pending.clear();
    this.child.kill();
    this.exited();
  }
  private send(value: unknown) {
    if (this.ended) throw Error("Backend is not running.");
    const line = JSON.stringify(value) + "\n";
    if (Buffer.byteLength(line) > 16 * 1024 * 1024)
      throw Error("Request exceeds protocol limit.");
    this.child.stdin.write(line);
  }
  private consume(chunk: string) {
    this.buffer += chunk;
    if (Buffer.byteLength(this.buffer) > 16 * 1024 * 1024) {
      this.fail();
      return;
    }
    let index: number;
    while ((index = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      try {
        const value: unknown = JSON.parse(line);
        if (!value || typeof value !== "object" || Array.isArray(value))
          throw Error();
        const msg = value as RecordValue;
        if (msg.jsonrpc !== "2.0") throw Error();
        this.dispatch(msg);
      } catch {
        this.fail();
        return;
      }
    }
  }
  private dispatch(msg: RecordValue) {
    if (typeof msg.method === "string") {
      const params =
        msg.params &&
        typeof msg.params === "object" &&
        !Array.isArray(msg.params)
          ? (msg.params as RecordValue)
          : {};
      if (msg.id !== undefined) {
        const id = msg.id;
        if (typeof id !== "string") throw Error("Invalid callback id.");
        void this.callback(msg.method, params).then(
          (result) => {
            if (!this.ended) this.send({ jsonrpc: "2.0", id, result });
          },
          () => {
            if (!this.ended)
              this.send({
                jsonrpc: "2.0",
                id,
                error: { code: -32000, message: "Editor operation failed." },
              });
          },
        );
      } else if (msg.method === "agent/event") this.event(params);
    } else if (typeof msg.id === "number") {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error && typeof msg.error === "object") {
        const e = msg.error as RecordValue;
        p.reject(
          Error(
            typeof e.message === "string"
              ? e.message
              : "Backend request failed.",
          ),
        );
      } else p.resolve(msg.result);
    }
  }
  request(method: string, params: RecordValue = {}): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const id = ++this.serial;
      this.pending.set(id, { resolve, reject });
      try {
        this.send({ jsonrpc: "2.0", id, method, params });
      } catch (e) {
        this.pending.delete(id);
        reject(e);
      }
    });
  }
  dispose() {
    this.fail();
  }
  get running() {
    return !this.ended;
  }
}
