import { useEffect, useState } from "react";

async function fetchChangelog(): Promise<string> {
  const res = await fetch("/api/changelog");
  if (!res.ok) throw new Error("Failed to load changelog");
  const data = (await res.json()) as { content: string };
  return data.content;
}

function renderMarkdown(md: string): JSX.Element[] {
  const lines = md.split("\n");
  const elements: JSX.Element[] = [];
  let key = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("## ")) {
      elements.push(<h2 key={key++}>{line.slice(3)}</h2>);
    } else if (line.startsWith("### ")) {
      elements.push(<h3 key={key++}>{line.slice(4)}</h3>);
    } else if (line.startsWith("# ")) {
      elements.push(<h1 key={key++}>{line.slice(2)}</h1>);
    } else if (line.startsWith("---")) {
      elements.push(<hr key={key++} />);
    } else if (line.startsWith("- ")) {
      const items: JSX.Element[] = [];
      let j = i;
      while (j < lines.length && lines[j].startsWith("- ")) {
        items.push(<li key={j} dangerouslySetInnerHTML={{ __html: inlineFormat(lines[j].slice(2)) }} />);
        j++;
      }
      elements.push(<ul key={key++}>{items}</ul>);
      i = j - 1;
    } else if (line.trim() === "") {
      // skip blank lines
    } else {
      elements.push(<p key={key++} dangerouslySetInnerHTML={{ __html: inlineFormat(line) }} />);
    }
  }

  return elements;
}

function inlineFormat(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

export function ChangelogModal({ onClose }: { onClose: () => void }) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchChangelog()
      .then(setContent)
      .catch(() => setError("Could not load changelog."));
  }, []);

  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal changelog-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Release Notes</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="changelog-body">
          {error ? (
            <p className="empty">{error}</p>
          ) : content === null ? (
            <p className="empty">Loading…</p>
          ) : (
            renderMarkdown(content)
          )}
        </div>
      </div>
    </div>
  );
}
