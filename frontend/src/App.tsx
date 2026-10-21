import { ServerCrash } from "lucide-react";
import { Navigate, Route, Routes } from "react-router-dom";
import Header from "./components/Header";
import Logo from "./components/Logo";
import { Button, Spinner } from "./components/ui";
import { getDemo } from "./lib/demo";
import { useMeta } from "./lib/meta";
import Admin from "./pages/Admin";
import Chat from "./pages/Chat";
import NotFound from "./pages/NotFound";
import Welcome from "./pages/Welcome";

function Splash() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
      <Logo size={48} />
      <div className="flex items-center gap-2 text-sm text-muted">
        <Spinner className="text-brand" /> 正在连接演示服务…
      </div>
      <p className="max-w-xs text-xs text-subtle">
        演示服务空闲时会休眠，首次连接可能需要几秒钟。
      </p>
    </div>
  );
}

function Offline() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
      <ServerCrash size={40} className="text-bad" />
      <div>
        <h1 className="text-lg font-semibold">演示服务暂时没有响应</h1>
        <p className="mt-1 text-sm text-muted">服务可能正在重启，请稍后重试。</p>
      </div>
      <Button variant="primary" onClick={() => window.location.reload()}>
        重试
      </Button>
    </div>
  );
}

/** 演示模式下，访客需要先创建沙箱才能开始聊天。 */
function RequireSession({ children }: { children: JSX.Element }) {
  const { meta } = useMeta();
  if (meta?.auth_mode === "demo" && !getDemo()) return <Navigate to="/" replace />;
  return children;
}

/** 每次渲染时重新判断，使沙箱重置后能够回到此入口。 */
function Home() {
  const { meta } = useMeta();
  return meta?.auth_mode === "demo" && !getDemo() ? <Welcome /> : <Navigate to="/chat" replace />;
}

export default function App() {
  const { meta, loading, error } = useMeta();
  if (loading) return <Splash />;
  if (error || !meta) return <Offline />;

  return (
    <div className="flex h-full flex-col">
      <Header />
      <main className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route
            path="/chat"
            element={
              <RequireSession>
                <Chat />
              </RequireSession>
            }
          />
          <Route
            path="/console"
            element={
              <RequireSession>
                <Admin />
              </RequireSession>
            }
          />
          <Route path="/admin" element={<Navigate to="/console" replace />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
