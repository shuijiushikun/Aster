import clsx from "clsx";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type Kind = "info" | "success" | "error";
interface Toast {
  id: number;
  kind: Kind;
  text: string;
}

const Ctx = createContext<(text: string, kind?: Kind) => void>(() => {});

export const useToast = () => useContext(Ctx);

export function Toaster({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: Kind = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-2), { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);
  const Icon = { info: Info, success: CheckCircle2, error: AlertTriangle };
  return (
    <Ctx.Provider value={push}>
      {children}
      <div
        aria-live="polite"
        className="pointer-events-none fixed bottom-4 left-1/2 z-50 flex w-[min(28rem,92vw)] -translate-x-1/2 flex-col gap-2"
      >
        {toasts.map((t) => {
          const I = Icon[t.kind];
          return (
            <div
              key={t.id}
              className="pointer-events-auto flex animate-fade-up items-start gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-sm shadow-lg"
            >
              <I
                size={18}
                className={clsx(
                  "mt-0.5 shrink-0",
                  { info: "text-info", success: "text-ok", error: "text-bad" }[t.kind],
                )}
              />
              <span className="flex-1">{t.text}</span>
              <button
                aria-label="关闭提示"
                onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}
                className="text-subtle hover:text-fg"
              >
                <X size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </Ctx.Provider>
  );
}
