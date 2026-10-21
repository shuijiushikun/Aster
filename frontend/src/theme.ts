import { useEffect, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "acme.theme";

function current(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

/** 管理浅色和深色主题；初始主题由 index.html 在页面首次绘制前应用。 */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(current);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);
  const toggle = () =>
    setTheme((t) => {
      const next = t === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, next);
      } catch {
        /* 存储不可用时，不持久化主题设置。 */
      }
      return next;
    });
  return [theme, toggle];
}
