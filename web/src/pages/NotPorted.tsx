/* A page this app does not have yet says so and links to the console that
 * does. It shows no data of its own. */
export function NotPorted({ title, slice, href }: { title: string; slice: string; href: string }) {
  return (
    <section className="flex max-w-xl flex-col gap-2 py-6">
      <h1 className="text-[14px] tracking-[1.5px] text-accent">{title.toUpperCase()}</h1>
      <p className="text-text-muted">
        {title} is ported in slice {slice} of docs/UI_PORT.md. Until then it lives in the{" "}
        <a className="text-accent underline" href={href}>
          current console
        </a>
        .
      </p>
    </section>
  );
}
