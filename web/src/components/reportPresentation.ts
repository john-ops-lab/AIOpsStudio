/** Derive display excerpts only. Persisted report and message text stay untouched. */
function conclusionText(text: string): string {
  const headings: { start: number; body: number; level: number; conclusion: boolean }[] = [];
  const boldParagraphs: number[] = [];
  let fence = "";
  for (const match of text.matchAll(/^.*$/gm)) {
    const line = match[0], marker = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1][0];
      else if (marker[1][0] === fence) fence = "";
      continue;
    }
    if (fence) continue;
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const label = heading[2].replace(/\*\*|__/g, "").replace(/^\d+[.、)）]\s*/, "").trim();
      headings.push({ start: match.index, body: match.index + line.length, level: heading[1].length, conclusion: /^(?:分析)?结论(?:$|\s|[：:（(]|与|及|和)/u.test(label) });
    }
    if (/^\s*(?:>\s*)*(?:[-*+]\s+)?\*\*(?:分析)?结论(?:[：:]|\*\*)/u.test(line)) boldParagraphs.push(match.index);
  }
  const section = headings.filter((heading) => heading.conclusion).at(-1);
  const boldStart = boldParagraphs.at(-1);
  if (boldStart !== undefined) {
    const rest = text.slice(boldStart);
    return rest.split(/\r?\n[ \t]*(?:>[ \t]*)?\r?\n/, 1)[0].trim();
  }
  if (section) {
    const end = headings.find((heading) => heading.start > section.start && heading.level <= section.level)?.start ?? text.length;
    const body = text.slice(section.body, end).trim();
    if (body) return body;
  }
  return text.trim();
}
/** Tables, code and detail lists belong in the complete reply and report canvas. */
function summaryParagraphs(text: string): string {
  const paragraphs: string[] = [], lines = text.split(/\r?\n/);
  let current: string[] = [], fence = "", table = false, list = false;
  const flush = () => { if (current.length) paragraphs.push(current.join("\n")); current = []; };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], marker = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (marker) { flush(); fence = fence ? (marker[1][0] === fence ? "" : fence) : marker[1][0]; continue; }
    if (fence) continue;
    if (/^\s*(?:>\s*)?$/.test(line)) { flush(); table = false; list = false; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) || (line.includes("|") && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i + 1] || ""))) table = true;
    if (table && line.includes("|")) { flush(); continue; }
    table = false;
    if (/^\s{0,3}#{1,6}\s+/.test(line)) { flush(); list = false; continue; }
    if (/^\s*(?:[-*+]|\d+[.)、])\s+/.test(line) && !/\*\*(?:分析)?结论(?:[：:]|\*\*)/u.test(line)) list = true;
    if (list) { flush(); continue; }
    current.push(line);
  }
  flush();
  return paragraphs.join("\n\n").trim();
}
function safeBoundary(text: string): boolean {
  if (text.lastIndexOf("[ev_") > text.lastIndexOf("]")) return false;
  let fence = "";
  for (const line of text.split(/\r?\n/)) {
    const marker = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (marker) fence = fence ? (marker[1][0] === fence ? "" : fence) : marker[1][0];
  }
  if (fence) return false;
  return (text.match(/\*\*/g)?.length || 0) % 2 === 0 && (text.match(/(?<!`)`(?!`)/g)?.length || 0) % 2 === 0;
}
/** Keep whole paragraphs or sentences; never slice a word, sentence, or evidence ID. */
export function reportSummary(fullText: string, limit = 360): string {
  const candidate = summaryParagraphs(conclusionText(fullText));
  if (!candidate) return "请展开查看完整回复中的结论与明细。";
  if (candidate.length <= limit) return candidate;
  const boundaries: number[] = [];
  for (const match of candidate.matchAll(/\r?\n[ \t]*\r?\n/g)) boundaries.push(match.index);
  for (const match of candidate.matchAll(/[。！？!?](?:[”’"')）】]|\*\*|__|~~)*|\.(?=\s|$)/gu)) {
    let end = match.index + match[0].length;
    // A citation immediately following a sentence belongs to that sentence.
    let reference: RegExpMatchArray | null;
    while ((reference = candidate.slice(end).match(/^\s*\[ev_[a-f0-9]{32}\]/))) end += reference[0].length;
    boundaries.push(end);
  }
  const end = boundaries.filter((offset) => offset <= limit && safeBoundary(candidate.slice(0, offset))).sort((a, b) => b - a)[0];
  const excerpt = end ? candidate.slice(0, end).trim() : "";
  return excerpt && excerpt.replace(/^\s*#{1,6}\s+.*$/gm, "").trim() ? excerpt : "回复较长，请展开查看完整内容。";
}
export const reportMetadataTitles: Record<string, string> = {
  target: "目标对象", source: "报告来源", model_generated: "模型生成标记",
  completion_scope: "完成范围", source_limits: "源查询范围与限制",
  reference_validation: "证据引用校验", business_acceptance: "业务验收状态",
  root_cause_confirmation: "根因确认状态", termination_reason: "结束原因",
  model: "模型信息", usage: "调用用量", budget: "执行预算", created_at: "生成时间",
};
