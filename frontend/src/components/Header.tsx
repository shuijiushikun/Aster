import clsx from "clsx";
import { Github, Moon, RotateCcw, Sun } from "lucide-react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { REPO_URL } from "../config";
import { clearDemo, getDemo } from "../lib/demo";
import { useMeta } from "../lib/meta";
import { useTheme } from "../theme";
import DevIdentity from "./DevIdentity";
import Logo from "./Logo";
import { Badge, IconButton } from "./ui";

function Tab({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        clsx(
          "rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
          isActive ? "bg-brand/10 text-brand" : "text-muted hover:bg-surface-2 hover:text-fg",
        )
      }
    >
      {label}
    </NavLink>
  );
}

export default function Header() {
  const { meta } = useMeta();
  const [theme, toggleTheme] = useTheme();
  const navigate = useNavigate();
  const demo = meta?.auth_mode === "demo" ? getDemo() : null;

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-surface/80 backdrop-blur">
      <div className="flex min-h-14 flex-wrap items-center gap-x-2 gap-y-2 px-3 py-2 sm:gap-3 sm:px-5">
        <Link to="/" className="flex items-center gap-2.5" title="返回首页">
          <Logo size={30} />
          <div className="hidden leading-tight sm:block">
            <div className="text-sm font-semibold">ACME 客服</div>
            <div className="text-[11px] text-subtle">智能退款助手</div>
          </div>
        </Link>

        <nav className="ml-2 flex items-center gap-1 sm:ml-4">
          <Tab to="/chat" label="客服对话" />
          <Tab to="/console" label="管理控制台" />
        </nav>

        <div className="ml-auto flex w-full items-center justify-end gap-1.5 sm:w-auto">
          {meta?.auth_mode === "dev" && <DevIdentity />}
          {meta && (
            <Badge tone={meta.model === "scripted" ? "neutral" : "brand"} className="hidden md:inline-flex">
              {meta.model === "scripted" ? "脚本演示模型" : meta.model_name || meta.model}
            </Badge>
          )}
          {demo && (
            <button
              onClick={() => {
                clearDemo();
                navigate("/");
              }}
              className="inline-flex items-center gap-1.5 rounded-full border border-warn/30 bg-warn/10 px-2 py-1 text-[11px] font-semibold text-warn transition-colors hover:bg-warn/20 sm:px-2.5"
              title="重置当前演示环境并重新开始"
              aria-label="重置演示环境"
            >
              <span className="hidden sm:inline">重置演示</span> <RotateCcw size={12} />
            </button>
          )}
          <IconButton className="hidden sm:inline-flex" label="查看 GitHub 源码" onClick={() => window.open(REPO_URL, "_blank")}>
            <Github size={16} />
          </IconButton>
          <IconButton
            label={theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            onClick={toggleTheme}
          >
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </IconButton>
        </div>
      </div>
    </header>
  );
}
