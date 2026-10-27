import { MessageSquare, RefreshCw } from "lucide-react";
import { formatDate, formatTime } from "../lib/locale";
import type { ConversationSummary } from "../types";
import { Button, Skeleton } from "./ui";

export default function ConversationHistory({ rows, currentId, loading, error, onSelect, onRefresh }: {
  rows: ConversationSummary[]; currentId: string | null; loading: boolean; error: string | null;
  onSelect: (id: string) => void; onRefresh: () => void;
}) {
  return <div className="space-y-4 p-5">
    <div className="flex items-center justify-between"><p className="text-sm text-muted">对话保存在当前账户下，可随时继续。</p><Button onClick={onRefresh} disabled={loading} size="sm"><RefreshCw size={14} />刷新</Button></div>
    {error && <p role="alert" className="text-sm text-bad">{error}</p>}
    {loading && <Skeleton className="h-20" />}
    {!loading && !rows.length && !error && <p className="py-8 text-center text-sm text-subtle">还没有历史对话，发送一条消息开始咨询。</p>}
    {rows.map((row) => <button key={row.id} aria-pressed={currentId === row.id} onClick={() => onSelect(row.id)} disabled={loading}
      className={`w-full rounded-xl border p-4 text-left transition-colors ${currentId === row.id ? "border-brand bg-brand/5" : "border-line bg-surface hover:bg-surface-2"}`}>
      <div className="flex items-center gap-2 text-sm font-semibold"><MessageSquare size={16} className="shrink-0 text-brand" /><span className="line-clamp-2 break-words">{row.last_message || "售后咨询"}</span></div>
      <p className="mt-3 text-sm text-subtle">{formatDate(row.created_at)} {formatTime(row.created_at)} · {row.message_count} 条消息{currentId === row.id ? " · 当前对话" : ""}</p>
    </button>)}
  </div>;
}
