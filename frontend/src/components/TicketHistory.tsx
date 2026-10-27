import { formatDate, formatTime } from "../lib/locale";
import { ticketEventText } from "../lib/tickets";
import type { TicketDetail } from "../types";

export default function TicketHistory({ events, technical = false }: { events: TicketDetail["events"]; technical?: boolean }) {
  return <section className="space-y-4" aria-label="工单处理记录">
    <h3 className="text-base font-semibold">处理记录 <span className="font-normal text-subtle">· {events.length} 条</span></h3>
    <ol className="space-y-5">
      {events.map((event) => {
        const row = ticketEventText(event);
        return <li key={event.id} className="relative border-l-2 border-line pl-4 text-sm leading-relaxed">
          <span className="absolute -left-[5px] top-1.5 h-2 w-2 rounded-full bg-brand" />
          <div className="flex flex-wrap items-center justify-between gap-2"><p className="font-semibold">{row.title}</p><span className="text-sm text-subtle">{row.actor} · {formatDate(event.created_at)} {formatTime(event.created_at)}</span></div>
          <p className="mt-1 break-words text-muted">{row.description}</p>
          {technical && <details className="mt-2 text-sm text-subtle"><summary className="cursor-pointer">技术详情</summary><p className="mt-2 font-mono">{event.kind} · {event.actor_type}</p><pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-surface-2 p-3">{JSON.stringify(event.payload_json, null, 2)}</pre></details>}
        </li>;
      })}
    </ol>
  </section>;
}
