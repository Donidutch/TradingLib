import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

// Colours belong in tokens.css. Anything else in src/ must use var(--ds-*).
const SRC = join(import.meta.dirname, "..");
const TOKENS = join(import.meta.dirname, "tokens.css");
const RAW_COLOUR = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(/g;

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.(css|tsx?)$/.test(name) ? [path] : [];
  });
}

describe("design tokens", () => {
  it("keeps colour literals out of everything but tokens.css", () => {
    const offenders = files(SRC)
      .filter((path) => path !== TOKENS && path !== import.meta.filename)
      .flatMap((path) =>
        readFileSync(path, "utf8")
          .split("\n")
          .flatMap((line, i) =>
            line.match(RAW_COLOUR) ? [`${relative(SRC, path)}:${i + 1}: ${line.trim()}`] : [],
          ),
      );
    expect(offenders).toEqual([]);
  });

  it("defines every token var() refers to", () => {
    const defined = new Set(readFileSync(TOKENS, "utf8").match(/--ds-[\w-]+(?=:)/g));
    const used = files(SRC).flatMap((path) =>
      [...readFileSync(path, "utf8").matchAll(/var\((--ds-[\w-]+)/g)].map((m) => m[1] ?? ""),
    );
    expect(used.filter((name) => !defined.has(name))).toEqual([]);
  });
});
