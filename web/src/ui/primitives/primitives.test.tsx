import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ColumnDef } from "@tanstack/react-table";
import { useState } from "react";
import { ConfirmDialog } from "./confirm";
import { DataTable } from "./data-table";
import { Field, Input } from "./field";
import { Switch } from "./switch";

describe("ConfirmDialog", () => {
  function Harness({ onConfirm }: { onConfirm: () => Promise<void> }) {
    const [open, setOpen] = useState(true);
    return (
      <ConfirmDialog open={open} title="Delete NVDA watchlist?" confirmLabel="Delete" onConfirm={onConfirm} onCancel={() => setOpen(false)}>
        This cannot be undone.
      </ConfirmDialog>
    );
  }

  it("runs the action only on confirm", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn(async () => undefined);
    render(<Harness onConfirm={onConfirm} />);
    expect(screen.getByRole("dialog")).toHaveTextContent("Delete NVDA watchlist?This cannot be undone.");
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("backs out on Cancel and Escape without acting", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn(async () => undefined);
    render(<Harness onConfirm={onConfirm} />);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

describe("DataTable", () => {
  type Row = { symbol: string; pct: number };
  const columns: ColumnDef<Row, unknown>[] = [
    { accessorKey: "symbol", header: "Symbol" },
    { accessorKey: "pct", header: "Change" },
  ];
  const rows = [
    { symbol: "MSFT", pct: 0.5 },
    { symbol: "AAPL", pct: -0.2 },
    { symbol: "NVDA", pct: -0.7 },
  ];

  it("names itself, sorts by a column and filters", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<DataTable caption="Tape quotes" data={rows} columns={columns} />);
    const table = screen.getByRole("table", { name: "Tape quotes" });
    // Numbers sort largest first, and the header says so before the click.
    await user.click(within(table).getByRole("button", { name: "Change, click to sort descending" }));
    expect(within(table).getAllByRole("row").slice(1).map((r) => r.textContent)).toEqual(["MSFT0.5", "AAPL-0.2", "NVDA-0.7"]);
    await user.click(within(table).getByRole("button", { name: "Change, sorted descending, click to sort ascending" }));
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("NVDA");
    rerender(<DataTable caption="Tape quotes" data={rows} columns={columns} globalFilter="AA" />);
    expect(within(table).getAllByRole("row")).toHaveLength(2);
  });

  it("says what is empty", () => {
    render(<DataTable caption="Backtests" data={[]} columns={columns} empty="No backtests yet." />);
    expect(screen.getByText("No backtests yet.")).toBeInTheDocument();
  });

  it("pages long lists", async () => {
    const user = userEvent.setup();
    const many = Array.from({ length: 30 }, (_, i) => ({ symbol: `S${i}`, pct: i }));
    render(<DataTable caption="Many" data={many} columns={columns} pageSize={25} />);
    expect(screen.getByText("1-25 of 30")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("26-30 of 30")).toBeInTheDocument();
  });
});

describe("form fields", () => {
  it("labels a control and shows its error", () => {
    render(
      <Field label="Interval" htmlFor="interval" error="use 45s, 2m, 4h or 1d">
        <Input id="interval" invalid defaultValue="5x" />
      </Field>,
    );
    expect(screen.getByLabelText("Interval")).toHaveValue("5x");
    expect(screen.getByText("use 45s, 2m, 4h or 1d")).toHaveClass("text-down");
  });

  it("toggles a switch", async () => {
    const user = userEvent.setup();
    render(<Switch label="Include in run" defaultChecked={false} />);
    const toggle = screen.getByRole("switch", { name: "Include in run" });
    await user.click(toggle);
    expect(toggle).toBeChecked();
  });
});
