import { act, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { useToast, type Toaster } from "./toast-context";
import { ToastProvider } from "./ToastProvider";

function Grab({ onToast }: { onToast: (toast: Toaster) => void }) {
  const toast = useToast();
  useEffect(() => {
    onToast(toast);
  }, [toast, onToast]);
  return null;
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

it("shows one toast at a time, queues the rest and drops the oldest beyond four", () => {
  const box: { toast?: Toaster } = {};
  render(
    <ToastProvider>
      <Grab onToast={(t) => void (box.toast = t)} />
    </ToastProvider>,
  );
  const status = screen.getByRole("status");
  act(() => {
    box.toast?.ok("first");
    for (const n of [2, 3, 4, 5, 6]) box.toast?.show(`queued ${n}`);
  });
  expect(status).toHaveTextContent("first");
  expect(status.firstElementChild).toHaveAttribute("data-tone", "ok");

  const seen: string[] = [];
  for (let i = 0; i < 5; i++) {
    act(() => vi.advanceTimersByTime(2600));
    seen.push(status.textContent ?? "");
  }
  // "queued 2" was dropped: the backlog keeps the newest four.
  expect(seen).toEqual(["queued 3", "queued 4", "queued 5", "queued 6", ""]);
});

it("keeps errors up longer", () => {
  const box: { toast?: Toaster } = {};
  render(
    <ToastProvider>
      <Grab onToast={(t) => void (box.toast = t)} />
    </ToastProvider>,
  );
  act(() => box.toast?.error("broken"));
  act(() => vi.advanceTimersByTime(4000));
  expect(screen.getByRole("status")).toHaveTextContent("broken");
  act(() => vi.advanceTimersByTime(300));
  expect(screen.getByRole("status")).toHaveTextContent("");
});
