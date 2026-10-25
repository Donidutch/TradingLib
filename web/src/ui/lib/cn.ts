import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge class names with clsx, then resolve conflicting Tailwind utilities
 * with tailwind-merge (last-wins). The single class helper used by every
 * primitive in the design system.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
