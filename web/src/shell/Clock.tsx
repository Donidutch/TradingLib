import { useSettings } from "../api/hooks";
import { clockIn, etClock, zoneLabel } from "../ui/format";
import { useNow } from "./useNow";

/** The topbar clock, in the timezone chosen in Settings (New York until loaded). */
export function Clock() {
  const now = new Date(useNow(1000));
  const zone = useSettings().data?.timezone ?? "America/New_York";
  const time = clockIn(zone, now);
  // A zone the browser does not know falls back to New York, as the old console did.
  const [text, label, title] = time ? [time, zoneLabel(zone), zone] : [etClock(now), "ET", "America/New_York"];
  return (
    <span className="mono" aria-label="Clock" title={title}>
      {text} {label}
    </span>
  );
}
