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

  it("uses only colour utilities the theme defines", () => {
    // Tailwind drops an unknown class silently, so `text-red-500` or a typo
    // would render uncoloured instead of failing. Every colour utility must
    // name a --color-* from theme.css.
    const theme = readFileSync(join(import.meta.dirname, "theme.css"), "utf8");
    const colours = new Set([...theme.matchAll(/--color-([\w-]+):/g)].map((m) => m[1]));
    const notColours = new Set([
      // text-*: sizes and alignment; border-*: sides and styles; ring/outline: widths and styles
      "xs", "sm", "base", "lg", "xl", "left", "center", "right", "justify", "start", "end",
      "ellipsis", "clip", "wrap", "nowrap", "balance", "pretty",
      "t", "b", "l", "r", "x", "y", "s", "e", "solid", "dashed", "dotted", "double", "none",
      "hidden", "collapse", "separate", "offset", "inset",
    ]);
    const utility =
      /(?<![\w-])(?:[a-z-]+:)*(?:bg|text|border(?:-[trblxyse])?|ring(?:-offset)?|outline|divide|fill|stroke|placeholder|caret|decoration)-([a-z][a-z0-9-]*?)(?:\/\d+)?(?=["'`\s])/g;
    const offenders = files(SRC)
      .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path))
      .flatMap((path) =>
        [...readFileSync(path, "utf8").matchAll(utility)]
          // border-l-accent / ring-offset-bg: the colour follows the side or "offset".
          .map((m) => (m[1] ?? "").replace(/^(?:[trblxyse]|offset)-/, ""))
          .filter((name) => !colours.has(name) && !notColours.has(name) && !/^\d/.test(name))
          .map((name) => `${relative(SRC, path)}: ${name}`),
      );
    expect(offenders).toEqual([]);
  });
});
