import clsx from "clsx";
import { LayoutDashboard, MessageSquare, Moon, RotateCcw, Sun } from "lucide-react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { clearDemo, getDemo } from "../lib/demo";
import { useMeta } from "../lib/meta";
import { useTheme } from "../theme";
import DevIdentity from "./DevIdentity";
import Logo from "./Logo";
import { Badge, IconButton } from "./ui";

export default function Header() {
  const { meta } = useMeta();
  const [theme, toggleTheme] = useTheme();
  const navigate = useNavigate();
  const demo = meta?.auth_mode === "demo" ? getDemo() : null;
  return <>
    <aside className="app-rail" aria-label="主导航">
      <Link to="/" className="rail-logo" title="智能售后工作台首页"><Logo size={43} /></Link>
      <nav className="rail-nav">{([{ to: "/chat", label: "客户服务", icon: MessageSquare }, { to: "/console", label: "管理工作台", icon: LayoutDashboard }]).map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} title={label} aria-label={label} className={({ isActive }) => clsx("rail-item", isActive && "active")}><Icon size={22} strokeWidth={1.7} /><span>{label === "客户服务" ? "客服" : "管理"}</span></NavLink>)}</nav>
      <IconButton label={theme === "dark" ? "切换浅色主题" : "切换深色主题"} className="rail-theme" onClick={toggleTheme}>{theme === "dark" ? <Sun size={20} /> : <Moon size={20} />}</IconButton>
    </aside>
    <header className="app-topbar"><div className="min-w-0"><p className="text-sm font-semibold tracking-tight">智能售后工作台</p><p className="mt-0.5 hidden text-[10px] text-subtle sm:block">每一次咨询，都有清晰的下一步</p></div>
      <div className="ml-auto flex flex-wrap items-center justify-end gap-2">{meta?.auth_mode === "dev" && <DevIdentity />}
        {meta && <Badge tone="brand" className="hidden 2xl:inline-flex">{meta.model === "scripted" ? "脚本演示" : meta.model_name || meta.model}</Badge>}
        {demo && <button onClick={() => { clearDemo(); navigate("/"); }} className="flex items-center gap-1 rounded-full bg-warn/10 px-3 py-1.5 text-[11px] font-medium text-warn" aria-label="重置演示环境"><RotateCcw size={12} /> 重置演示</button>}
      </div>
    </header>
  </>;
}
