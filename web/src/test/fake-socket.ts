/** A WebSocket stand-in the tests drive by hand. */
export class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState: number = WebSocket.CONNECTING;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  static latest(): FakeSocket {
    const socket = FakeSocket.instances.at(-1);
    if (!socket) throw new Error("no socket opened");
    return socket;
  }

  send(raw: string) {
    this.sent.push(JSON.parse(raw) as Record<string, unknown>);
  }

  close(code = 1000, reason = "") {
    this.readyState = WebSocket.CLOSED;
    this.onclose?.({ code, reason });
  }

  // --- server side
  open() {
    this.readyState = WebSocket.OPEN;
    this.onopen?.();
  }

  frame(body: unknown) {
    this.onmessage?.({ data: JSON.stringify(body) });
  }

  event(channel: string, payload: unknown) {
    this.frame({ type: "event", channel, payload, ts: 0, id: "x" });
  }

  drop(code = 1006) {
    this.readyState = WebSocket.CLOSED;
    this.onclose?.({ code, reason: "" });
  }
}

export const fakeSocketFactory = (url: string) => new FakeSocket(url) as unknown as WebSocket;
