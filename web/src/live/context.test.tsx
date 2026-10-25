import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { Notification } from "../api/schemas/stream";
import { useChannel, useStreamSnapshot } from "./context";
import { FakeSocket, fakeSocketFactory } from "../test/fake-socket";
import { LiveStream } from "./stream";
import { StreamProvider } from "./StreamProvider";

beforeEach(() => {
  FakeSocket.instances = [];
});

it("connects on mount, reports state and delivers channel events", () => {
  const stream = new LiveStream({ url: "ws://test/ws", socketFactory: fakeSocketFactory });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <StreamProvider stream={stream}>{children}</StreamProvider>
  );
  const messages: string[] = [];
  const { result, unmount } = renderHook(
    () => {
      useChannel("notifications", Notification, (n) => messages.push(n.message));
      return useStreamSnapshot();
    },
    { wrapper },
  );
  expect(result.current.state).toBe("connecting");

  act(() => FakeSocket.latest().open());
  expect(result.current.state).toBe("open");
  expect(FakeSocket.latest().sent).toEqual([{ action: "subscribe", channel: "notifications", params: {} }]);

  act(() => FakeSocket.latest().event("notifications", { kind: "finding", message: "Finding created: X" }));
  expect(messages).toEqual(["Finding created: X"]);

  unmount();
  expect(stream.snapshot().state).toBe("closed");
});
