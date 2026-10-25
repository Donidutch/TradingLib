import { toneClass, type Indicator } from "./health";

export function EngineIndicator({ engine }: { engine: Indicator }) {
  return (
    <span className={toneClass[engine.tone]} title={engine.title} aria-label="Engine health">
      {engine.text}
    </span>
  );
}
