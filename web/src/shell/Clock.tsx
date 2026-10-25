import { etClock } from "../ui/format";
import { useNow } from "./useNow";

export function Clock() {
  const now = useNow(1000);
  return (
    <span className="mono" aria-label="New York time">
      {etClock(new Date(now))} ET
    </span>
  );
}
