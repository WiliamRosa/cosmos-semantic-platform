/**
 * The Cosmos Semantic icon: the brand mark on a navy rounded square, the same in light and dark
 * themes. Generated from marketing/brand/cosmos-semantic-icon.svg (see marketing/brand/README.md);
 * regenerate it from there instead of editing the shapes by hand.
 */
export function Logo({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 100 100" className={className} aria-hidden="true">
      <rect width="100" height="100" rx="22" fill="#0b1220"/>
      <g transform="translate(-1.149 -0.240) scale(3.19680)">
      <defs>
      <clipPath id="front-cs-logo"><rect x="-8" y="16" width="48" height="24" transform="rotate(-24 16 16)"/></clipPath>
      <mask id="planet-cs-logo" maskUnits="userSpaceOnUse" x="-8" y="-8" width="48" height="48">
      <rect x="-8" y="-8" width="48" height="48" fill="#fff"/>
      <g clipPath="url(#front-cs-logo)"><ellipse cx="16" cy="16" rx="11.4" ry="3.5" fill="none" stroke="#000" strokeWidth="2.70" transform="rotate(-24 16 16)"/></g>
      </mask>
      <mask id="moon-cs-logo" maskUnits="userSpaceOnUse" x="-8" y="-8" width="48" height="48">
      <rect x="-8" y="-8" width="48" height="48" fill="#fff"/>
      <circle cx="25.299" cy="10.549" r="2.10" fill="#000"/>
      </mask>
      </defs>
      <g mask="url(#moon-cs-logo)"><ellipse cx="16" cy="16" rx="11.4" ry="3.5" fill="none" stroke="#f8fafc" strokeWidth="1.50" transform="rotate(-24 16 16)"/></g>
      <circle cx="16" cy="16" r="6.0" fill="#38bdf8" mask="url(#planet-cs-logo)"/>
      <g clipPath="url(#front-cs-logo)"><g mask="url(#moon-cs-logo)"><ellipse cx="16" cy="16" rx="11.4" ry="3.5" fill="none" stroke="#f8fafc" strokeWidth="1.50" transform="rotate(-24 16 16)"/></g></g>
      <circle cx="25.299" cy="10.549" r="1.5" fill="#34d399"/>
      </g>
    </svg>
  );
}
