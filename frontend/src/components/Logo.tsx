/** 售后助手标识。 */
export default function Logo({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <rect width="64" height="64" rx="19" fill="rgb(var(--c-brand-solid))" />
      <path
        d="M32 13c1 12 6 17 18 19-12 2-17 7-18 19-2-12-7-17-19-19 12-2 17-7 19-19Z M48 10v10 M43 15h10"
        stroke="rgb(var(--c-on-brand))"
        strokeWidth="3"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
