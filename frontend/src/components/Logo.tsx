// The mark: an armadillo's shell, drawn with the current text colour so it follows the theme.
// Decorative next to the name, so hidden from screen readers.
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true" className="logo">
      <rect width="32" height="32" rx="9" fill="var(--accent)" />
      <path
        d="M6 21c0-6.1 4.9-11 11-11h3c3.3 0 6 2.7 6 6v5H6Z"
        fill="var(--accent-text)"
        opacity=".95"
      />
      <path d="M11 21c0-4.4 2.2-8.2 5-10M16 21c0-4.8 1.6-8.7 4-11M21 21c0-4.6.9-8 2.6-10" stroke="var(--accent)" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M6 21h20" stroke="var(--accent)" strokeWidth="1.4" />
      <path d="M8.5 24h15" stroke="var(--accent-text)" strokeWidth="2" strokeLinecap="round" opacity=".7" />
    </svg>
  )
}
