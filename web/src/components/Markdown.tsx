import { useMemo, useRef } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
};
const evidencePattern = /\[(ev_[a-f0-9]{32}(?:#frag_[a-f0-9]{32})?)\]/g;
/** Annotate complete citations in parsed text only; leave source and code blocks intact. */
function remarkEvidenceReferences() {
  return function walk(node: MarkdownNode) {
    if (
      !node.children ||
      ["link", "linkReference", "code", "inlineCode", "html"].includes(
        node.type,
      )
    )
      return;
    const children: MarkdownNode[] = [];
    for (const child of node.children) {
      if (child.type !== "text" || !child.value) {
        walk(child);
        children.push(child);
        continue;
      }
      let offset = 0;
      for (const match of child.value.matchAll(evidencePattern)) {
        if (match.index > offset)
          children.push({
            type: "text",
            value: child.value.slice(offset, match.index),
          });
        children.push({
          type: "link",
          url: `#aiopsstudio-evidence/${match[1]}`,
          children: [{ type: "text", value: match[0] }],
        });
        offset = match.index + match[0].length;
      }
      if (offset < child.value.length)
        children.push({ type: "text", value: child.value.slice(offset) });
    }
    node.children = children;
  };
}
const plugins = [remarkGfm, remarkEvidenceReferences];
/** Render persisted text without raw HTML or automatic remote image requests. */
export default function Markdown({
  text,
  onEvidence,
}: {
  text: string;
  onEvidence?: (id: string) => void;
}) {
  const callback = useRef(onEvidence);
  callback.current = onEvidence;
  const interactive = Boolean(onEvidence);
  const components = useMemo<Components>(
    () => ({
      a: ({ children, href }) => {
        const reference = href?.match(/^#aiopsstudio-evidence\/(ev_[a-f0-9]{32}(?:#frag_[a-f0-9]{32})?)$/);
        if (reference)
          return interactive ? (
            <button
              type="button"
              className="text-link evidence-reference"
              aria-label={`查看证据 ${reference[1]}`}
              onClick={() => callback.current?.(reference[1])}
            >
              {children}
            </button>
          ) : (
            <>{children}</>
          );
        return (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        );
      },
    }),
    [interactive],
  );
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={plugins}
        skipHtml
        disallowedElements={["img"]}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
