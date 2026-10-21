/** 退款智能体标识：深色底上的青柠色账本勾选图案，在两种主题中保持一致。 */
export default function Logo({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <rect width="64" height="64" rx="16" fill="#0a0b0d" />
      <rect x="6" y="6" width="52" height="52" rx="12" fill="none" stroke="#c8f560" strokeWidth="3.5" />
      <path
        d="m20 33 8.5 8.5L45 24"
        stroke="#c8f560"
        strokeWidth="5"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
