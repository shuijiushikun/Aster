"""One adapter for graph/subgraph updates; intermediate JSON is never a reply."""

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Overwrite


def message_text(message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(
        block.get("text", "")
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class GraphEvents:
    def __init__(self, *, v2: bool):
        self.v2 = v2
        self.seen_messages = set()
        self.seen_domain = set()

    def adapt(self, chunk):
        namespace, data = chunk if isinstance(chunk, tuple) else ((), chunk)
        for node, update in data.items():
            if not isinstance(update, dict):
                continue
            path = "/".join([part.split(":")[0] for part in namespace] + [node])
            for key in (
                "messages",
                "order_messages",
                "knowledge_messages",
                "router_messages",
                "proposal_messages",
            ):
                messages = update.get(key, [])
                if isinstance(messages, Overwrite):
                    messages = messages.value
                for message in messages:
                    if not isinstance(message, (AIMessage, ToolMessage)):
                        continue
                    identity = message.id or (
                        getattr(message, "tool_call_id", None),
                        message.type,
                        str(message.content),
                    )
                    if identity in self.seen_messages:
                        continue
                    self.seen_messages.add(identity)
                    yield "message", path, message
                    if (
                        not self.v2
                        and isinstance(message, AIMessage)
                        and message_text(message).strip()
                    ):
                        yield "final", path, message_text(message)
            # Only the outer graph can commit the user-visible answer.
            if not namespace and update.get("final_response"):
                yield "final", path, update["final_response"]
            for kind, key in (
                ("ticket_update", "ticket_result"),
                ("knowledge_sources", "knowledge_evidence"),
                ("workflow_status", "workflow_result"),
            ):
                value = update.get(key)
                if namespace or not value:
                    continue
                event = (
                    {"kind": kind, **value}
                    if kind == "workflow_status"
                    else {"kind": kind, "ticket_id": value["id"], "status": value["status"]}
                    if kind == "ticket_update"
                    else {"kind": kind, "sources": value.get("sources", [])}
                )
                identity = repr(event)
                if identity not in self.seen_domain:
                    self.seen_domain.add(identity)
                    yield "domain", path, event
