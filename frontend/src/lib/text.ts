/** 将 Markdown 转为单行纯文本，用于无法渲染 Markdown 的预览区域。 */
export function plainText(md: string | null | undefined): string {
  if (!md) return "";
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // 链接和图片只保留说明文字。
    .replace(/(\*\*|\*|~~|`)(?=\S)([\s\S]*?\S)\1/g, "$2") // 移除强调和代码标记；保留 ID 中可能使用的下划线。
    .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+)/gm, "") // 移除块级标记。
    .replace(/\s+/g, " ")
    .trim();
}
