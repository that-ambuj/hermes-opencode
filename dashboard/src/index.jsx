(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const cn =
    (SDK.utils && SDK.utils.cn) ||
    function () {
      return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
    };

  const PHASE_GLYPH = {
    EXECUTING: "▶",
    EXECUTOR_ADDRESSING: "▶",
    IDLE_TASK_COMPLETE: "⏸",
    IDLE_REVIEW_ADDRESSED: "⏸",
    REVIEW_SPAWNING: "🔎",
    REVIEWING: "🔎",
    REVIEW_DELIVERED: "🔎",
    COMMITTING: "💾",
    PR_OPEN: "🔗",
    DONE: "✓",
    FAILED: "✗",
    KILLED: "🛑",
  };

  async function api(path, options) {
    const token = window.__HERMES_SESSION_TOKEN__ || "";
    const headers = Object.assign({}, (options && options.headers) || {});
    if (token) headers["X-Hermes-Session-Token"] = token;
    const res = await fetch("/api/plugins/opencode-orchestrator" + path, {
      ...(options || {}),
      headers,
    });
    if (!res.ok) {
      const text = await res
        .text()
        .catch(() => res.statusText);
      throw new Error(res.status + ": " + text);
    }
    const text = await res.text();
    try {
      return JSON.parse(text);
    } catch (_) {
      return null;
    }
  }

  function formatAge(ts) {
    if (!ts) return "—";
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h + "h " + m + "m";
  }

  function PhaseCell({ phase }) {
    const g = PHASE_GLYPH[phase] || "•";
    return (
      <span className={cn("oco-phase", "oco-phase-" + phase)}>
        <span className="oco-glyph">{g}</span>{" "}
        <span className="oco-phase-label">{phase}</span>
      </span>
    );
  }

  function AgentsTable({ agents, onSelect }) {
    if (!agents || agents.length === 0) {
      return <div className="oco-empty">No agents tracked.</div>;
    }
    return (
      <div className="oco-table-wrap">
        <table className="oco-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Project</th>
              <th>Branch</th>
              <th>Phase</th>
              <th>PR</th>
              <th>Last activity</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr
                key={a.agent_id}
                className="oco-row-clickable"
                onClick={() => onSelect && onSelect(a.agent_id)}
                title="click to inspect"
              >
                <td className="oco-mono oco-link-cell">{a.agent_id}</td>
                <td>{a.project_label}</td>
                <td className="oco-mono">{a.branch}</td>
                <td>
                  <PhaseCell phase={a.phase} />
                </td>
                <td onClick={(e) => e.stopPropagation()}>
                  {a.pr_url ? (
                    <a href={a.pr_url} target="_blank" rel="noopener noreferrer">
                      #{a.pr_number || ""}
                    </a>
                  ) : (
                    "—"
                  )}
                </td>
                <td>{formatAge(a.last_activity_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  function DetailRow({ label, value, mono, children }) {
    if (!children && (value === null || value === undefined || value === "")) return null;
    return (
      <div className="oco-detail-row">
        <div className="oco-detail-label">{label}</div>
        <div className={cn("oco-detail-value", mono && "oco-mono")}>
          {children || value}
        </div>
      </div>
    );
  }

  function AgentDetailModal({ agent, onClose }) {
    React.useEffect(() => {
      function onKey(e) {
        if (e.key === "Escape") onClose();
      }
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    }, [onClose]);

    if (!agent) return null;
    const mergedAt = agent.pr_merged_at
      ? new Date(agent.pr_merged_at * 1000).toLocaleString()
      : null;
    const doneAt = agent.done_at
      ? new Date(agent.done_at * 1000).toLocaleString()
      : null;
    const createdAt = agent.created_at
      ? new Date(agent.created_at * 1000).toLocaleString()
      : null;

    return (
      <div
        className="oco-modal-backdrop"
        onClick={onClose}
        role="presentation"
      >
        <div
          className="oco-modal"
          role="dialog"
          aria-modal="true"
          aria-label={"Agent " + agent.agent_id}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="oco-modal-header">
            <div className="oco-modal-title">
              <span className="oco-mono">{agent.agent_id}</span>
              <PhaseCell phase={agent.phase} />
            </div>
            <button
              type="button"
              className="oco-modal-close"
              onClick={onClose}
              aria-label="close"
            >
              ✕
            </button>
          </div>
          <div className="oco-modal-body">
            <DetailRow label="Project" value={agent.project_label} />
            <DetailRow label="Branch" value={agent.branch} mono />
            <DetailRow label="Session" value={agent.session_id} mono />
            <DetailRow label="Worktree" value={agent.worktree_path} mono />
            {agent.reviewer_session_id && (
              <DetailRow
                label="Reviewer session"
                value={agent.reviewer_session_id}
                mono
              />
            )}
            {agent.reviewer_worktree_path && (
              <DetailRow
                label="Reviewer worktree"
                value={agent.reviewer_worktree_path}
                mono
              />
            )}
            {agent.review_cycle_count !== undefined && (
              <DetailRow
                label="Review cycles"
                value={String(agent.review_cycle_count)}
              />
            )}
            <DetailRow label="Created" value={createdAt} />
            {createdAt && (
              <DetailRow
                label="Age"
                value={formatAge(agent.created_at) + " ago"}
              />
            )}
            <DetailRow
              label="Last activity"
              value={formatAge(agent.last_activity_at) + " ago"}
            />
            <DetailRow label="PR">
              {agent.pr_url ? (
                <a
                  href={agent.pr_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {agent.pr_url}
                </a>
              ) : (
                "—"
              )}
            </DetailRow>
            {agent.pr_number && (
              <DetailRow label="PR number" value={"#" + agent.pr_number} />
            )}
            {mergedAt && <DetailRow label="Merged at" value={mergedAt} />}
            {doneAt && <DetailRow label="Done at" value={doneAt} />}
            {agent.last_error && (
              <DetailRow label="Last error">
                <pre className="oco-detail-error">{agent.last_error}</pre>
              </DetailRow>
            )}
            <DetailRow label="Initial prompt">
              <pre className="oco-detail-prompt">
                {agent.initial_prompt || "—"}
              </pre>
            </DetailRow>
          </div>
          <div className="oco-modal-footer">
            <span className="oco-modal-hint">
              press Esc or click outside to close
            </span>
          </div>
        </div>
      </div>
    );
  }

  function ProjectsTable({ projects }) {
    if (!projects || projects.length === 0) {
      return <div className="oco-empty">No projects registered.</div>;
    }
    return (
      <div className="oco-table-wrap">
        <table className="oco-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Abbrev</th>
              <th>Repo path</th>
              <th>Base branch</th>
              <th>Bootstrap skill</th>
            </tr>
          </thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.label}>
                <td className="oco-mono">{p.label}</td>
                <td className="oco-mono">{p.abbrev}</td>
                <td
                  className={cn(
                    "oco-mono",
                    "oco-truncate",
                    !p.repo_exists && "oco-warn"
                  )}
                  title={p.repo_path}
                >
                  {p.repo_path}
                </td>
                <td className="oco-mono">{p.base_branch}</td>
                <td
                  className="oco-mono oco-truncate"
                  title={p.bootstrap_skill || ""}
                >
                  {p.bootstrap_skill || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  function HeartbeatsList({ items }) {
    if (!items || items.length === 0) {
      return <div className="oco-empty">No heartbeats yet.</div>;
    }
    return (
      <div className="oco-heartbeats">
        {items.map((h, i) => {
          const when =
            h.meta && h.meta.when
              ? h.meta.when
              : h.ts
              ? new Date(h.ts * 1000).toLocaleString()
              : "";
          return (
            <div key={i} className="oco-heartbeat">
              <div className="oco-heartbeat-when">{when}</div>
              <pre className="oco-heartbeat-body">{h.body || ""}</pre>
            </div>
          );
        })}
      </div>
    );
  }

  function RefreshButton({ onClick, refreshing, lastRefreshAt }) {
    const tooltip = lastRefreshAt
      ? "last refresh " + formatAge(lastRefreshAt) + " ago"
      : "refresh now";
    return (
      <button
        type="button"
        className={cn("oco-refresh", refreshing && "oco-refresh-spinning")}
        onClick={onClick}
        disabled={refreshing}
        title={tooltip}
      >
        <span className="oco-refresh-icon" aria-hidden="true">
          ↻
        </span>
        {refreshing ? "refreshing…" : "refresh"}
      </button>
    );
  }

  function OpencodeAgentsPage() {
    const [agents, setAgents] = React.useState([]);
    const [projects, setProjects] = React.useState([]);
    const [heartbeats, setHeartbeats] = React.useState([]);
    const [error, setError] = React.useState(null);
    const [, setLoading] = React.useState(true);
    const [refreshing, setRefreshing] = React.useState(false);
    const [lastRefreshAt, setLastRefreshAt] = React.useState(null);
    const [selectedAgentId, setSelectedAgentId] = React.useState(null);
    const selectedAgent = React.useMemo(
      () => agents.find((a) => a.agent_id === selectedAgentId) || null,
      [agents, selectedAgentId]
    );

    const refresh = React.useCallback(async function () {
      setRefreshing(true);
      try {
        const [a, p, h] = await Promise.all([
          api("/agents"),
          api("/projects"),
          api("/heartbeats?n=5"),
        ]);
        setAgents((a && a.agents) || []);
        setProjects((p && p.projects) || []);
        setHeartbeats((h && h.items) || []);
        setError(null);
        setLastRefreshAt(Date.now() / 1000);
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    }, []);

    React.useEffect(
      function () {
        refresh();
        const id = setInterval(refresh, 5000);
        return function () {
          clearInterval(id);
        };
      },
      [refresh]
    );

    return (
      <div className="oco-page">
        <header className="oco-header">
          <div className="oco-header-row">
            <h1>Opencode Agents</h1>
            <RefreshButton
              onClick={refresh}
              refreshing={refreshing}
              lastRefreshAt={lastRefreshAt}
            />
          </div>
          <div className="oco-stats">
            <span>
              {agents.length} agent{agents.length === 1 ? "" : "s"}
            </span>
            <span className="oco-sep">·</span>
            <span>
              {projects.length} project{projects.length === 1 ? "" : "s"}
            </span>
            {lastRefreshAt && <span className="oco-sep">·</span>}
            {lastRefreshAt && (
              <span className="oco-loading">
                updated {formatAge(lastRefreshAt)} ago
              </span>
            )}
          </div>
          {error && <div className="oco-error">{error}</div>}
        </header>
        <section className="oco-section">
          <h2>Agents</h2>
          <AgentsTable agents={agents} onSelect={setSelectedAgentId} />
        </section>
        <section className="oco-section">
          <h2>Projects</h2>
          <ProjectsTable projects={projects} />
        </section>
        <section className="oco-section">
          <h2>Recent heartbeats</h2>
          <HeartbeatsList items={heartbeats} />
        </section>
        {selectedAgent && (
          <AgentDetailModal
            agent={selectedAgent}
            onClose={() => setSelectedAgentId(null)}
          />
        )}
      </div>
    );
  }

  window.__HERMES_PLUGINS__.register("opencode-orchestrator", OpencodeAgentsPage);
})();
