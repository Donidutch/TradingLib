/* Market & Data's tabs, shared by the page, the router and the hash map. */
export const DATA_TABS = [
  { value: "collections", label: "Universes & Watchlists" },
  { value: "screener", label: "Screener" },
  { value: "source-catalog", label: "Source Catalog" },
  { value: "ingestion", label: "Data Ingestion" },
  { value: "quality", label: "Data Quality" },
] as const;

export type DataTab = (typeof DATA_TABS)[number]["value"];

export const isDataTab = (value: string | undefined): value is DataTab => DATA_TABS.some((t) => t.value === value);
