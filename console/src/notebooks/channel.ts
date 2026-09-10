import { KernelMessage } from "@jupyterlab/services";
import {
  serialize,
  deserialize,
} from "@jupyterlab/services/lib/kernel/serialize";
import { notebookTicket, type NotebookContext } from "../api";

const PROTOCOL = "v1.kernel.websocket.jupyter.org";
type Pending = {
  id: string;
  output: (msg: KernelMessage.IMessage) => void;
  resolve: (reply: KernelMessage.IExecuteReplyMsg) => void;
  reject: (error: Error) => void;
  reply?: KernelMessage.IExecuteReplyMsg;
  idle: boolean;
  bytes: number;
};
/** A single-use connection. Reconnect is always an explicit user action. */
export class NotebookChannel {
  private pending?: Pending;
  private session = crypto.randomUUID();
  private intentional = false;
  private constructor(
    private socket: WebSocket,
    private lost: (message: string) => void,
  ) {
    socket.binaryType = "arraybuffer";
    socket.onmessage = ({ data }) => {
      try {
        if (!(data instanceof ArrayBuffer) || data.byteLength > 2 * 1024 * 1024)
          throw new Error("Invalid notebook frame");
        const message = deserialize(data, PROTOCOL);
        const p = this.pending;
        if (!p || message.parent_header.msg_id !== p.id) return;
        if (message.header.msg_type === "execute_reply")
          p.reply = message as KernelMessage.IExecuteReplyMsg;
        else if (
          message.header.msg_type === "status" &&
          (message as KernelMessage.IStatusMsg).content.execution_state ===
            "idle"
        )
          p.idle = true;
        else if (
          [
            "stream",
            "display_data",
            "execute_result",
            "error",
            "clear_output",
            "update_display_data",
            "execute_input",
          ].includes(message.header.msg_type)
        ) {
          p.bytes += data.byteLength;
          if (p.bytes > 1024 * 1024)
            throw new Error("Cell output limit exceeded");
          p.output(message);
        }
        if (p.reply && p.idle) {
          this.pending = undefined;
          p.resolve(p.reply);
        }
      } catch (e) {
        this.fail(e instanceof Error ? e.message : "Invalid kernel message");
      }
    };
    socket.onclose = () =>
      this.fail(
        "Kernel channel closed. In-flight results may be incomplete. Reconnect explicitly; cells will not be replayed.",
      );
    socket.onerror = () =>
      this.fail(
        "Kernel channel failed. Reconnect explicitly; cells will not be replayed.",
      );
  }
  static async connect(
    context: NotebookContext,
    lost: (message: string) => void,
    signal: AbortSignal,
  ): Promise<NotebookChannel> {
    const ticket = await notebookTicket(context.id, context.generation);
    signal.throwIfAborted();
    if (ticket.protocol !== PROTOCOL)
      throw new Error("Unsupported notebook protocol");
    const url = new URL(
      `/api/notebooks/${context.id}/${context.generation}/channels`,
      location.origin,
    );
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url, [
      ticket.protocol,
      ticket.authorization_protocol,
    ]);
    const channel = new NotebookChannel(socket, lost);
    try {
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(
          () => finish(new Error("Kernel connection timed out")),
          10000,
        );
        const abort = () => finish(new Error("Connection cancelled"));
        const open = () =>
          finish(
            socket.protocol === PROTOCOL
              ? undefined
              : new Error("Kernel protocol mismatch"),
          );
        const error = () => finish(new Error("Kernel connection failed"));
        function finish(e?: Error) {
          clearTimeout(timeout);
          signal.removeEventListener("abort", abort);
          socket.removeEventListener("open", open);
          socket.removeEventListener("error", error);
          socket.removeEventListener("close", error);
          e ? reject(e) : resolve();
        }
        signal.addEventListener("abort", abort, { once: true });
        socket.addEventListener("open", open, { once: true });
        socket.addEventListener("error", error, { once: true });
        socket.addEventListener("close", error, { once: true });
      });
      signal.throwIfAborted();
      return channel;
    } catch (e) {
      channel.dispose();
      throw e;
    }
  }
  get ready() {
    return this.socket.readyState === WebSocket.OPEN && !this.intentional;
  }
  execute(
    code: string,
    output: Pending["output"],
  ): Promise<KernelMessage.IExecuteReplyMsg> {
    if (!this.ready || this.pending)
      return Promise.reject(
        new Error("Kernel is disconnected or already executing"),
      );
    if (new TextEncoder().encode(code).length > 65536)
      return Promise.reject(
        new Error("Cell source exceeds the 64 KiB execution limit"),
      );
    const msg = KernelMessage.createMessage({
      msgType: "execute_request",
      channel: "shell",
      session: this.session,
      content: {
        code,
        silent: false,
        store_history: true,
        user_expressions: {},
        allow_stdin: false,
        stop_on_error: true,
      },
    });
    return new Promise((resolve, reject) => {
      this.pending = {
        id: msg.header.msg_id,
        output,
        resolve,
        reject,
        idle: false,
        bytes: 0,
      };
      try {
        this.socket.send(serialize(msg, PROTOCOL));
      } catch (e) {
        this.pending = undefined;
        reject(e);
      }
    });
  }
  private fail(message: string) {
    if (this.intentional) return;
    this.pending?.reject(new Error(message));
    this.pending = undefined;
    this.intentional = true;
    this.socket.close();
    this.lost(message);
  }
  dispose() {
    this.intentional = true;
    this.pending?.reject(
      new Error("Execution connection closed; results may be incomplete"),
    );
    this.pending = undefined;
    this.socket.close();
  }
}
