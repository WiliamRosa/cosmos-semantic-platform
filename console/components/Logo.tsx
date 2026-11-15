/** The Cosmos Semantic mark: a planet with an orbit, in the current text color. */
export function Logo({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <rect width="32" height="32" rx="8" className="fill-slate-900 dark:fill-white" />
      <circle cx="16" cy="16" r="5.5" className="fill-sky-400 dark:fill-sky-500" />
      <ellipse
        cx="16"
        cy="16"
        rx="11"
        ry="4.2"
        transform="rotate(-24 16 16)"
        fill="none"
        strokeWidth="1.6"
        className="stroke-white dark:stroke-slate-900"
      />
      <circle cx="25.4" cy="11.6" r="1.6" className="fill-white dark:fill-slate-900" />
    </svg>
  );
}
