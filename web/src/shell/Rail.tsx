import { Link, useRouterState } from "@tanstack/react-router";
import { cn } from "../ui/lib/cn";
import { WORKSPACES, type Workspace } from "./pages";

const item = "flex items-center gap-2 rounded-[var(--ds-radius-card)] px-2.5 py-2 text-text-muted select-none hover:bg-elevated";
const SECTIONS = ["RESEARCH", "PLATFORM"] as const;

function isActive(workspace: Workspace, pathname: string): boolean {
  if (workspace.path === "/") return pathname === "/";
  return Boolean(workspace.path && pathname.startsWith(workspace.path));
}

function RailItem({ workspace, pathname }: { workspace: Workspace; pathname: string }) {
  const label = (
    <>
      <span aria-hidden>{workspace.glyph}</span> {workspace.label}
    </>
  );
  if (workspace.path) {
    const active = isActive(workspace, pathname);
    return (
      <Link
        to={workspace.path}
        aria-current={active ? "page" : undefined}
        className={cn(item, active && "bg-active text-accent")}
      >
        {label}
      </Link>
    );
  }
  // Not ported yet: the page still lives in the vanilla console.
  return (
    <a href={`/${workspace.legacyHash ?? ""}`} title="Opens in the current console" className={item}>
      {label}
      <span className="ml-auto text-[10px] text-text-subtle" aria-hidden>
        ↗
      </span>
    </a>
  );
}

export function Rail() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return (
    <nav
      aria-label="Workspaces"
      className="flex w-[var(--ds-rail-w)] shrink-0 flex-col gap-0.5 border-r border-border-subtle bg-surface px-2 py-3"
    >
      {SECTIONS.map((section) => (
        <div key={section} className="flex flex-col gap-0.5">
          <div className="mx-2 mb-1 mt-2.5 text-[10px] tracking-[1px] text-text-subtle">{section}</div>
          {WORKSPACES.filter((w) => w.section === section).map((workspace) => (
            <RailItem key={workspace.key} workspace={workspace} pathname={pathname} />
          ))}
        </div>
      ))}
    </nav>
  );
}
