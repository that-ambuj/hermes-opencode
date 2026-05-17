(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const C = SDK.components || {};
  const cn = (SDK.utils && SDK.utils.cn) || function () {
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
      const text = await res.text().catch(function () { return res.statusText; });
      throw new Error(res.status + ": " + text);
    }
    const text = await res.text();
    try { return JSON.parse(text); } catch (_) { return null; }
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
    return React.createElement("span", { className: cn("oco-phase", "oco-phase-" + phase) },
      React.createElement("span", { className: "oco-glyph" }, g),
      " ",
      React.createElement("span", { className: "oco-phase-label" }, phase),
    );
  }

  function AgentsTable({ agents }) {
    if (!agents || agents.length === 0) {
      return React.createElement("div", { className: "oco-empty" }, "No agents tracked.");
    }
    return React.createElement("div", { className: "oco-table-wrap" },
      React.createElement("table", { className: "oco-table" },
        React.createElement("thead", null,
          React.createElement("tr", null,
            React.createElement("th", null, "Agent"),
            React.createElement("th", null, "Project"),
            React.createElement("th", null, "Branch"),
            React.createElement("th", null, "Phase"),
            React.createElement("th", null, "PR"),
            React.createElement("th", null, "Last activity"),
          )
        ),
        React.createElement("tbody", null,
          agents.map(function (a) {
            return React.createElement("tr", { key: a.agent_id },
              React.createElement("td", { className: "oco-mono" }, a.agent_id),
              React.createElement("td", null, a.project_label),
              React.createElement("td", { className: "oco-mono" }, a.branch),
              React.createElement("td", null, React.createElement(PhaseCell, { phase: a.phase })),
              React.createElement("td", null, a.pr_url
                ? React.createElement("a", { href: a.pr_url, target: "_blank", rel: "noopener noreferrer" }, "#" + (a.pr_number || ""))
                : "—"),
              React.createElement("td", null, formatAge(a.last_activity_at)),
            );
          })
        )
      )
    );
  }

  function ProjectsTable({ projects }) {
    if (!projects || projects.length === 0) {
      return React.createElement("div", { className: "oco-empty" }, "No projects registered.");
    }
    return React.createElement("div", { className: "oco-table-wrap" },
      React.createElement("table", { className: "oco-table" },
        React.createElement("thead", null,
          React.createElement("tr", null,
            React.createElement("th", null, "Label"),
            React.createElement("th", null, "Abbrev"),
            React.createElement("th", null, "Repo path"),
            React.createElement("th", null, "Base branch"),
            React.createElement("th", null, "Bootstrap skill"),
          )
        ),
        React.createElement("tbody", null,
          projects.map(function (p) {
            return React.createElement("tr", { key: p.label },
              React.createElement("td", { className: "oco-mono" }, p.label),
              React.createElement("td", { className: "oco-mono" }, p.abbrev),
              React.createElement("td", {
                className: cn("oco-mono", "oco-truncate", !p.repo_exists && "oco-warn"),
                title: p.repo_path,
              }, p.repo_path),
              React.createElement("td", { className: "oco-mono" }, p.base_branch),
              React.createElement("td", { className: "oco-mono oco-truncate", title: p.bootstrap_skill || "" },
                p.bootstrap_skill || "—"),
            );
          })
        )
      )
    );
  }

  function HeartbeatsList({ items }) {
    if (!items || items.length === 0) {
      return React.createElement("div", { className: "oco-empty" }, "No heartbeats yet.");
    }
    return React.createElement("div", { className: "oco-heartbeats" },
      items.map(function (h, i) {
        const when = h.meta && h.meta.when ? h.meta.when : (h.ts ? new Date(h.ts * 1000).toLocaleString() : "");
        return React.createElement("div", { key: i, className: "oco-heartbeat" },
          React.createElement("div", { className: "oco-heartbeat-when" }, when),
          React.createElement("pre", { className: "oco-heartbeat-body" }, h.body || ""),
        );
      })
    );
  }

  function OpencodeAgentsPage() {
    const [agents, setAgents] = React.useState([]);
    const [projects, setProjects] = React.useState([]);
    const [heartbeats, setHeartbeats] = React.useState([]);
    const [error, setError] = React.useState(null);
    const [loading, setLoading] = React.useState(true);

    const refresh = React.useCallback(async function () {
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
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    }, []);

    React.useEffect(function () {
      refresh();
      const id = setInterval(refresh, 5000);
      return function () { clearInterval(id); };
    }, [refresh]);

    return React.createElement("div", { className: "oco-page" },
      React.createElement("header", { className: "oco-header" },
        React.createElement("h1", null, "Opencode Agents"),
        React.createElement("div", { className: "oco-stats" },
          React.createElement("span", null, agents.length + " agent" + (agents.length === 1 ? "" : "s")),
          React.createElement("span", { className: "oco-sep" }, "·"),
          React.createElement("span", null, projects.length + " project" + (projects.length === 1 ? "" : "s")),
          loading && React.createElement("span", { className: "oco-loading" }, "loading…"),
        ),
        error && React.createElement("div", { className: "oco-error" }, error),
      ),
      React.createElement("section", { className: "oco-section" },
        React.createElement("h2", null, "Agents"),
        React.createElement(AgentsTable, { agents: agents }),
      ),
      React.createElement("section", { className: "oco-section" },
        React.createElement("h2", null, "Projects"),
        React.createElement(ProjectsTable, { projects: projects }),
      ),
      React.createElement("section", { className: "oco-section" },
        React.createElement("h2", null, "Recent heartbeats"),
        React.createElement(HeartbeatsList, { items: heartbeats }),
      )
    );
  }

  window.__HERMES_PLUGINS__.register("opencode-orchestrator", OpencodeAgentsPage);
})();
