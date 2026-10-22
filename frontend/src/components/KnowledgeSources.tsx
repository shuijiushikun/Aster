import { ArrowUpRight, BookOpen } from "lucide-react";
import type { KnowledgeSource } from "../types";

function httpsUrl(value: string): string | null {
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : null; } catch { return null; }
}

export default function KnowledgeSources({ sources, received }: { sources: KnowledgeSource[]; received: boolean }) {
  return <section aria-label="知识来源" className="space-y-3"><h2 className="flex items-center gap-2 text-sm font-semibold"><BookOpen size={16} className="text-brand" /> 本轮知识来源 <span className="ml-auto text-[11px] font-normal text-subtle">{sources.length} 条引用</span></h2>
    {!sources.length && <div className="rounded-2xl bg-surface-2 p-4 text-xs leading-relaxed text-muted">{received ? "本轮没有返回可引用的知识证据。请提供更具体的商品或问题信息。" : "尚无知识引用。咨询商品或政策后，这里会展示实际检索到的文档片段。"}</div>}
    {sources.map((source, i) => <details key={`${source.document_id}-${source.revision}-${source.chunk_index}-${i}`} className="source-card" open={i === 0}><summary className="cursor-pointer text-xs font-semibold leading-relaxed">{source.title || source.document_id}<span className="mt-1 block font-mono text-[10px] font-normal text-subtle">{source.document_id} · v{source.revision}{source.chunk_index !== undefined ? ` · 片段 ${source.chunk_index}` : ""}</span></summary><p className="mt-3 whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">{source.excerpt || "无摘录"}</p>
      {source.references?.map(httpsUrl).filter((url): url is string => Boolean(url)).map((url) => <a key={url} href={url} target="_blank" rel="noopener noreferrer" className="mt-3 inline-flex items-center gap-1 break-all text-[11px] text-brand hover:underline">查看参考资料 <ArrowUpRight size={12} /></a>)}
    </details>)}
  </section>;
}
