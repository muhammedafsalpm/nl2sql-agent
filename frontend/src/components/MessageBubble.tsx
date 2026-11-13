import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message } from "../types";
import { Chart } from "./Chart";
import { ExportMenu } from "./ExportMenu";
import { SqlDrawer } from "./SqlDrawer";

interface Props {
  message: Message;
  /** The question this answer came from — names the exported file. */
  question?: string;
  /** Present only on a fast answer that can be re-asked on the thorough path. */
  onDeepSearch?: () => void;
}

export function MessageBubble({ message, question, onDeepSearch }: Props) {
  const { role, text, chart, sql, latency, pending, error, verified, mode, exportToken } =
    message;

  if (role === "user") {
    return (
      <div className="row row-user">
        <div className="bubble bubble-user">{text}</div>
      </div>
    );
  }

  return (
    <div className="row row-assistant">
      <div className={`bubble bubble-assistant${error ? " bubble-error" : ""}`}>
        {pending ? (
          <div className="pending">
            <span className="dots" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <span>{pending}</span>
          </div>
        ) : (
          <>
            {verified === false && (
              <div className="unverified" role="status">
                ⚠ These figures could not be fully verified — treat them as indicative.
              </div>
            )}
            <div className="markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
            </div>
            {chart && <Chart spec={chart} />}
            <div className="answer-tools">
              {sql && sql.length > 0 && <SqlDrawer statements={sql} />}
              {/* Token presence alone gates this: it is null on chitchat/clarify and on any turn
                  that retrieved nothing, so no route check is needed. Independent of the SQL
                  drawer, which is off by default. */}
              {!error && exportToken && (
                <ExportMenu
                  token={exportToken}
                  question={question ?? ""}
                  answer={text}
                  verified={verified !== false}
                />
              )}
            </div>
            {latency !== undefined && (
              <div className="meta">
                {mode && (
                  <span className={`badge badge-${mode}`}>
                    {mode === "deep" ? "Deep Search" : "Fast"}
                  </span>
                )}
                {latency.toFixed(1)}s
                {onDeepSearch && (
                  <button className="rerun" onClick={onDeepSearch}>
                    Re-run with Deep Search
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
