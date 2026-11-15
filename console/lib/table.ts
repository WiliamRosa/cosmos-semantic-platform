// Sorting and paging of lists in the browser (the catalog, usage tables), and the shared
// page sizes. Lists the server pages (answers, check runs) only use the page math.

export type SortDir = "asc" | "desc";

export interface Sort<K extends string = string> {
  key: K;
  dir: SortDir;
}

export const PAGE_SIZES = [10, 25, 50, 100] as const;

/** Sorts a copy; nulls and empty values always go last, whatever the direction. */
export function sortRows<T>(
  rows: T[],
  sort: Sort | null,
  value: (row: T, key: string) => string | number | null | undefined,
): T[] {
  if (!sort) return rows;
  const factor = sort.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = value(a, sort.key);
    const y = value(b, sort.key);
    const xEmpty = x === null || x === undefined || x === "";
    const yEmpty = y === null || y === undefined || y === "";
    if (xEmpty || yEmpty) return xEmpty === yEmpty ? 0 : xEmpty ? 1 : -1;
    if (typeof x === "number" && typeof y === "number") return (x - y) * factor;
    return String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: "base" }) * factor;
  });
}

/** The page count for `total` items, at least 1, and `page` clamped into it. */
export function pageInfo(total: number, page: number, size: number) {
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, page), pages);
  const offset = (current - 1) * size;
  return { pages, page: current, offset, from: total ? offset + 1 : 0, to: Math.min(offset + size, total) };
}

export function paginate<T>(rows: T[], page: number, size: number) {
  const info = pageInfo(rows.length, page, size);
  return { ...info, rows: rows.slice(info.offset, info.offset + size) };
}

/** Next sort when a column header is clicked: ascending, then descending, then back to ascending. */
export function nextSort<K extends string>(current: Sort<K> | null, key: K, firstDir: SortDir = "asc"): Sort<K> {
  if (current?.key !== key) return { key, dir: firstDir };
  return { key, dir: current.dir === "asc" ? "desc" : "asc" };
}

export function parseSort<K extends string>(raw: string | null, keys: readonly K[]): Sort<K> | null {
  if (!raw) return null;
  const desc = raw.startsWith("-");
  const key = (desc ? raw.slice(1) : raw) as K;
  return keys.includes(key) ? { key, dir: desc ? "desc" : "asc" } : null;
}

export function formatSort(sort: Sort | null): string | null {
  return sort ? `${sort.dir === "desc" ? "-" : ""}${sort.key}` : null;
}
