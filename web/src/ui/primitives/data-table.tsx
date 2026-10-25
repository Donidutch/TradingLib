import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  getFilteredRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";

interface DataTableProps<T> {
  data: T[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- TanStack columns are heterogeneous; `unknown` breaks accessor inference
  columns: ColumnDef<T, any>[];
  globalFilter?: string;
  onRowClick?: (row: T) => void;
  pageSize?: number;
  rowClassName?: (row: T) => string;
  /** Read by screen readers; name the table after what it lists. */
  caption: string;
  /** Shown in place of rows when there are none. */
  empty?: string;
}

export function DataTable<T>({
  data,
  columns,
  globalFilter,
  onRowClick,
  pageSize = 25,
  rowClassName,
  caption,
  empty = "No data",
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([]);

  // eslint-disable-next-line react-hooks/incompatible-library -- TanStack returns unmemoizable functions; React Compiler skips this component by design
  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    initialState: { pagination: { pageSize } },
  });

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <caption className="sr-only">{caption}</caption>
          <thead>
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b border-border">
                {hg.headers.map((header) => {
                  const canSort = header.column.getCanSort();
                  const sorted = header.column.getIsSorted();
                  const headerLabel =
                    typeof header.column.columnDef.header === "string"
                      ? header.column.columnDef.header
                      : header.id;
                  return (
                    <th
                      key={header.id}
                      scope="col"
                      onClick={canSort ? header.column.getToggleSortingHandler() : undefined}
                      className={`px-3 py-2 text-left text-text-muted uppercase tracking-wider font-medium select-none ${
                        canSort ? "cursor-pointer hover:text-text" : ""
                      }`}
                      {...(canSort
                        ? {
                            "aria-sort": sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none",
                            role: "button" as const,
                            // Say what the next click really does: number
                            // columns sort descending first.
                            "aria-label": `${headerLabel}${
                              sorted ? `, sorted ${sorted === "asc" ? "ascending" : "descending"}` : ""
                            }, click to ${
                              header.column.getNextSortingOrder() === "asc"
                                ? "sort ascending"
                                : header.column.getNextSortingOrder() === "desc"
                                  ? "sort descending"
                                  : "clear sort"
                            }`,
                          }
                        : {})}
                    >
                      <div className="flex items-center gap-1">
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sorted === "asc" && <ArrowUp size={10} aria-hidden="true" />}
                        {sorted === "desc" && <ArrowDown size={10} aria-hidden="true" />}
                        {!sorted && canSort && (
                          <ArrowUpDown size={10} className="opacity-30" aria-hidden="true" />
                        )}
                      </div>
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                onClick={() => onRowClick?.(row.original)}
                className={`border-b border-border-subtle transition-colors ${
                  onRowClick ? "cursor-pointer hover:bg-surface-2" : ""
                } ${rowClassName?.(row.original) ?? ""}`}
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-3 py-2">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
            {table.getRowModel().rows.length === 0 && (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-3 py-8 text-center text-text-subtle"
                >
                  {empty}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {table.getPageCount() > 1 && (
        <div className="flex items-center justify-between px-3 py-2 border-t border-border-subtle text-[10px] text-text-muted">
          <span>
            {table.getState().pagination.pageIndex * pageSize + 1}-
            {Math.min(
              (table.getState().pagination.pageIndex + 1) * pageSize,
              table.getFilteredRowModel().rows.length,
            )}{" "}
            of {table.getFilteredRowModel().rows.length}
          </span>
          <div className="flex gap-1">
            <button
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
              className="px-2 py-0.5 rounded bg-surface-2 hover:bg-border disabled:opacity-30 disabled:cursor-not-allowed"
            >
              Prev
            </button>
            <button
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
              className="px-2 py-0.5 rounded bg-surface-2 hover:bg-border disabled:opacity-30 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
