import { ArrowRight, Eye, KeyRound, Scale, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { friendlyError } from "../api";
import { useToast } from "../components/Toaster";
import { Button, Card, Spinner } from "../components/ui";
import { startDemo } from "../lib/demo";
import { useMeta } from "../lib/meta";

const FEATURES = [
  {
    icon: KeyRound,
    title: "身份来自已验证的登录凭证",
    text: "客户身份由签名凭证确定，聊天中的身份声明不会改变账户权限。",
  },
  {
    icon: Scale,
    title: "退款由确定性规则把关",
    text: "模型提出请求，代码决定结果。规则校验与数据库约束共同防止违规退款和重复退款。",
  },
  {
    icon: ShieldAlert,
    title: "识别可疑指令注入",
    text: "可疑指令会被标记，退款处理仍遵循既定业务规则。",
  },
  {
    icon: Eye,
    title: "执行过程实时可见",
    text: "工具调用、规则校验和处理结果会实时展示在执行面板与管理控制台。",
  },
];

export default function Welcome() {
  const [starting, setStarting] = useState(false);
  const navigate = useNavigate();
  const toast = useToast();
  const { meta } = useMeta();
  const ttl = meta?.demo?.data_ttl_hours ?? 24;
  const keepDays = meta?.demo?.transcript_retention_days ?? 90;

  async function start() {
    setStarting(true);
    try {
      await startDemo();
      navigate("/chat");
    } catch (err) {
      toast(friendlyError(err), "error");
      setStarting(false);
    }
  }

  return (
    <div className="flex h-full overflow-y-auto">
      <div className="relative m-auto flex max-w-5xl flex-col gap-10 px-5 py-12 lg:flex-row lg:items-center">
        <div className="relative flex-1 animate-fade-up">
          <span className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1 text-xs font-medium text-muted">
            <span className="h-1.5 w-1.5 rounded-full bg-ok" /> 在线演示 · 无需注册
          </span>
          <h1 className="mt-5 text-3xl font-bold tracking-tight sm:text-4xl">
            一个严格遵守退款规则的{" "}
            <span className="text-brand">
              智能客服助手。
            </span>
          </h1>
          <p className="mt-4 max-w-xl text-base text-muted">
            您将获得独立的演示账户和五笔订单，覆盖不同的退款规则。
            可以申请退款、尝试边界场景，并实时查看处理过程。
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Button variant="primary" size="lg" onClick={start} disabled={starting}>
              {starting ? <Spinner /> : null}
              {starting ? "正在创建演示环境…" : "开始体验"}
              {!starting && <ArrowRight size={16} />}
            </Button>
            <span className="max-w-xs text-xs text-subtle">
              演示数据将在 {ttl} 小时后清理，对话保留 {keepDays} 天用于安全分析，
              请勿输入真实个人信息。
            </span>
          </div>
        </div>

        <div className="relative grid flex-1 gap-3 sm:grid-cols-2">
          {FEATURES.map(({ icon: Icon, title, text }, i) => (
            <div key={title} className="animate-fade-up" style={{ animationDelay: `${i * 70}ms` }}>
              <Card className="h-full p-4">
                <div className="mb-3 flex h-9 w-9 items-center justify-center rounded-lg bg-brand/10 text-brand">
                  <Icon size={18} />
                </div>
                <h2 className="text-sm font-semibold">{title}</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted">{text}</p>
              </Card>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
