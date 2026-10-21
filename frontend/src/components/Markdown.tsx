import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 将助手消息渲染为 GitHub 风格的 Markdown，并适配聊天和管理界面的主题。
 * 项目未使用 Tailwind 排版插件，因此通过 `components` 映射逐个设置元素样式。
 * 除非显式覆盖，颜色均继承外围消息气泡的 text-fg 样式。
 */
export default function Markdown({ children }: { children: string }) {
  return (
    <div className="space-y-2 break-words leading-relaxed [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // 禁止渲染图片，避免根据模型输出加载第三方地址。
        disallowedElements={["img"]}
        unwrapDisallowed
        components={{
          p: ({ children }) => <p>{children}</p>,
          strong: ({ children }) => (
            <strong className="font-semibold text-fg">{children}</strong>
          ),
          em: ({ children }) => <em className="italic">{children}</em>,
          ul: ({ children }) => (
            <ul className="list-disc space-y-1 pl-5">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="list-decimal space-y-1 pl-5">{children}</ol>
          ),
          li: ({ children }) => <li className="pl-1">{children}</li>,
          h1: ({ children }) => (
            <h1 className="mt-3 mb-1 text-base font-semibold text-fg">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="mt-3 mb-1 text-base font-semibold text-fg">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="mt-2 mb-1 text-sm font-semibold text-fg">{children}</h3>
          ),
          // 链接仅显示为文本：此业务不需要智能体发送可点击链接，避免被操纵的输出
          // 将用户引向外部网站。同时展示 URL，避免链接文字掩盖实际地址。
          a: ({ children, href }) => {
            const text = typeof children === "string" ? children : null;
            return (
              <span>
                {children}
                {href && href !== text && (
                  <span className="break-all text-muted"> ({href})</span>
                )}
              </span>
            );
          },
          code: ({ className, children }) => {
            const isBlock = /language-/.test(className ?? "");
            if (isBlock) {
              return (
                <code className="block overflow-x-auto rounded-md bg-surface-2 p-3 font-mono text-xs">
                  {children}
                </code>
              );
            }
            return (
              <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[0.85em]">
                {children}
              </code>
            );
          },
          pre: ({ children }) => <pre className="my-2">{children}</pre>,
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-line pl-3 text-muted">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-3 border-line" />,
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-line px-2 py-1 text-left font-semibold">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-line px-2 py-1">{children}</td>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
