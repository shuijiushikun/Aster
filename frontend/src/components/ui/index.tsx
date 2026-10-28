import clsx from "clsx";
import { X } from "lucide-react";
import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode, type RefObject } from "react";
import type { Tone } from "../../lib/outcome";

type Variant = "primary" | "secondary" | "ghost";

export function Button({
  variant = "secondary",
  size = "md",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" | "lg" }) {
  return (
    <button
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-full font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        {
          sm: "min-h-10 px-3.5 py-2 text-sm",
          md: "min-h-11 px-4 py-2 text-sm",
          lg: "min-h-12 px-6 py-3 text-base",
        }[size],
        {
          primary: "bg-brand-solid font-semibold text-on-brand shadow-sm shadow-brand/20 hover:bg-brand-solid/90",
          secondary: "border border-line bg-surface text-fg hover:bg-surface-2",
          ghost: "text-muted hover:bg-surface-2 hover:text-fg",
        }[variant],
        className,
      )}
      {...props}
    />
  );
}

export function IconButton({
  label,
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      aria-label={label}
      title={label}
      className={clsx(
        "inline-flex h-10 w-10 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-fg",
        className,
      )}
      {...props}
    />
  );
}

const TONES: Record<Tone, string> = {
  ok: "border-ok/30 bg-ok/10 text-ok",
  bad: "border-bad/30 bg-bad/10 text-bad",
  warn: "border-warn/30 bg-warn/10 text-warn",
  info: "border-info/30 bg-info/10 text-info",
  brand: "border-brand/30 bg-brand/10 text-brand",
  neutral: "border-line bg-surface-2 text-muted",
};

export function Badge({
  tone = "neutral",
  children,
  className,
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-semibold",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={clsx("rounded-xl border border-line bg-surface shadow-sm", className)}>
      {children}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="加载中"
      className={clsx(
        "inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-r-transparent",
        className,
      )}
    />
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-md bg-surface-2", className)} />;
}

export function LiveDot({ className }: { className?: string }) {
  return (
    <span className={clsx("relative flex h-2 w-2", className)}>
      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />
      <span className="relative inline-flex h-2 w-2 rounded-full bg-current" />
    </span>
  );
}

function useModalFocus(open: boolean, panelRef: RefObject<HTMLElement>, onClose: () => void) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const focusable = () => Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]') ?? []).filter((element) => element.getClientRects().length > 0);
    focusable()[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
      if (e.key !== "Tab") return;
      const items = focusable();
      if (!items.length) { e.preventDefault(); panelRef.current?.focus(); return; }
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); if (previous?.getClientRects().length) previous.focus(); };
  }, [open, panelRef]);
}

/** 侧栏始终挂载，小屏仅切换可见性，保留工单中的未确认请求。 */
export function ResponsivePanel({ open, onClose, title, children, className }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode; className?: string;
}) {
  const [wide, setWide] = useState(() => window.matchMedia("(min-width: 1024px)").matches);
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const query = window.matchMedia("(min-width: 1024px)");
    const update = () => setWide(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  const modal = open && !wide;
  useModalFocus(modal, panelRef, onClose);
  return <>
    {modal && <div aria-hidden="true" className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm" onClick={onClose} />}
    <aside ref={panelRef} className={clsx(className, modal && "is-open")} role={modal ? "dialog" : undefined} aria-modal={modal || undefined} aria-hidden={!wide && !open} aria-label={title}>
      <div className="service-panel-mobile-header"><span className="text-sm font-semibold">{title}</span><IconButton label="关闭" onClick={onClose}><X size={16} /></IconButton></div>
      {children}
    </aside>
  </>;
}

/** 按需挂载的普通抽屉，用于不持有请求状态的订单列表。 */
export function Drawer({ open, onClose, title, children }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  useModalFocus(open, panelRef, onClose);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40" role="dialog" aria-modal="true" aria-label={title}>
      <div aria-hidden="true" className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div ref={panelRef} className="absolute inset-y-0 right-0 flex w-[min(26rem,92vw)] animate-slide-in flex-col border-l border-line bg-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <span className="text-sm font-semibold">{title}</span>
          <IconButton label="关闭" onClick={onClose}>
            <X size={16} />
          </IconButton>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const panelRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, panelRef, onClose);
  return <div className="fixed inset-0 z-50 grid place-items-center p-4" role="dialog" aria-modal="true" aria-label={title}>
    <div aria-hidden="true" className="absolute inset-0 bg-black/45 backdrop-blur-sm" onClick={onClose} />
    <div ref={panelRef} tabIndex={-1} className="relative max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-line bg-surface p-6 shadow-2xl sm:p-8">
      {children}
    </div>
  </div>;
}
