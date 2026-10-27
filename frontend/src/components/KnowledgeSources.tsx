import { ArrowUpRight, BookOpen } from "lucide-react";
import type { KnowledgeSource } from "../types";

function httpsUrl(value: string): string | null {
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : null; } catch { return null; }
}

export default function KnowledgeSources({ sources, received }: { sources: KnowledgeSource[]; received: boolean }) {
  return <section aria-label="知识来源" className="space-y-3"><h2 className="flex items-center gap-2 text-sm font-semibold"><BookOpen size={16} className="text-brand" /> 本轮知识来源 <span className="ml-auto text-xs font-normal text-subtle">{sources.length} 条引用</span></h2>
    {!sources.length && <div className="rounded-2xl bg-surface-2 p-4 text-sm leading-relaxed text-muted">{received ? "本轮没有取得可引用的资料，具体原因请看客服回复和上方处理记录。" : "咨询商品或政策后，这里会展示实际查到的文档片段。"}</div>}
    {sources.map((source, i) => <details key={`${source.document_id}-${source.revision}-${source.chunk_index}-${i}`} className="source-card" open={i === 0}><summary className="cursor-pointer text-sm font-semibold leading-relaxed">{source.title || source.document_id}<span className="mt-1 block font-mono text-xs font-normal text-subtle">{source.document_id} · v{source.revision}{source.chunk_index !== undefined ? ` · 片段 ${source.chunk_index}` : ""}</span></summary><p className="mt-3 whitespace-pre-wrap break-words text-sm leading-relaxed text-muted">{source.excerpt || "无摘录"}</p>
      {source.references?.map(httpsUrl).filter((url): url is string => Boolean(url)).map((url) => <a key={url} href={url} target="_blank" rel="noopener noreferrer" className="mt-3 inline-flex items-center gap-1 break-all text-xs text-brand hover:underline">编写参考来源（外部网站） <ArrowUpRight size={12} /></a>)}
    </details>)}
  </section>;
}
