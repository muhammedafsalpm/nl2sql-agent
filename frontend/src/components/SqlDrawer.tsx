import { useState } from "react";

/** Collapsed by default — end users should see answers, not internals. */
export function SqlDrawer({ statements }: { statements: string[] }) {
  const [open, setOpen] = useState(false);
  if (!statements.length) return null;

  return (
    <div className="sql-drawer">
      <button className="sql-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} {statements.length === 1 ? "SQL" : `SQL (${statements.length} queries)`}
      </button>
      {open &&
        statements.map((sql, i) => (
          <pre key={i} className="sql-block">
            <code>{sql}</code>
          </pre>
        ))}
    </div>
  );
}
