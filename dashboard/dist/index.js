(() => {
  (function() {
    "use strict";
    const SDK = window.__HERMES_PLUGIN_SDK__;
    if (!SDK || !window.__HERMES_PLUGINS__) return;
    const React = SDK.React;
    const cn = SDK.utils && SDK.utils.cn || function() {
      return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
    };
    const PHASE_GLYPH = {
      EXECUTING: "\u25B6",
      EXECUTOR_ADDRESSING: "\u25B6",
      IDLE_TASK_COMPLETE: "\u23F8",
      IDLE_REVIEW_ADDRESSED: "\u23F8",
      REVIEW_SPAWNING: "\u{1F50E}",
      REVIEWING: "\u{1F50E}",
      REVIEW_DELIVERED: "\u{1F50E}",
      COMMITTING: "\u{1F4BE}",
      PR_OPEN: "\u{1F517}",
      DONE: "\u2713",
      FAILED: "\u2717",
      KILLED: "\u{1F6D1}",
      CANCELLED: "\u{1F6AB}"
    };
    async function api(path, options) {
      const token = window.__HERMES_SESSION_TOKEN__ || "";
      const headers = Object.assign({}, options && options.headers || {});
      if (token) headers["X-Hermes-Session-Token"] = token;
      const res = await fetch("/api/plugins/hermes-opencode" + path, {
        ...options || {},
        headers
      });
      if (!res.ok) {
        const text2 = await res.text().catch(() => res.statusText);
        throw new Error(res.status + ": " + text2);
      }
      const text = await res.text();
      try {
        return JSON.parse(text);
      } catch (_) {
        return null;
      }
    }
    function formatAge(ts) {
      if (!ts) return "\u2014";
      const seconds = Math.max(0, Math.floor(Date.now() / 1e3 - ts));
      if (seconds < 60) return seconds + "s";
      if (seconds < 3600) return Math.floor(seconds / 60) + "m";
      const h = Math.floor(seconds / 3600);
      const m = Math.floor(seconds % 3600 / 60);
      return h + "h " + m + "m";
    }
    function PhaseCell({ phase }) {
      const g = PHASE_GLYPH[phase] || "\u2022";
      return /* @__PURE__ */ React.createElement("span", { className: cn("oco-phase", "oco-phase-" + phase) }, /* @__PURE__ */ React.createElement("span", { className: "oco-glyph" }, g), " ", /* @__PURE__ */ React.createElement("span", { className: "oco-phase-label" }, phase));
    }
    function AgentsTable({ agents, onSelect }) {
      if (!agents || agents.length === 0) {
        return /* @__PURE__ */ React.createElement("div", { className: "oco-empty" }, "No agents tracked.");
      }
      return /* @__PURE__ */ React.createElement("div", { className: "oco-table-wrap" }, /* @__PURE__ */ React.createElement("table", { className: "oco-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Agent"), /* @__PURE__ */ React.createElement("th", null, "Project"), /* @__PURE__ */ React.createElement("th", null, "Branch"), /* @__PURE__ */ React.createElement("th", null, "Phase"), /* @__PURE__ */ React.createElement("th", null, "PR"), /* @__PURE__ */ React.createElement("th", null, "Last activity"))), /* @__PURE__ */ React.createElement("tbody", null, agents.map((a) => /* @__PURE__ */ React.createElement(
        "tr",
        {
          key: a.agent_id,
          className: "oco-row-clickable",
          onClick: () => onSelect && onSelect(a.agent_id),
          title: "click to inspect"
        },
        /* @__PURE__ */ React.createElement("td", { className: "oco-mono oco-link-cell" }, a.agent_id, a.archived && /* @__PURE__ */ React.createElement("span", { className: "oco-archived-badge" }, "archived")),
        /* @__PURE__ */ React.createElement("td", null, a.project_label),
        /* @__PURE__ */ React.createElement("td", { className: "oco-mono" }, a.branch),
        /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement(PhaseCell, { phase: a.phase })),
        /* @__PURE__ */ React.createElement("td", { onClick: (e) => e.stopPropagation() }, a.pr_url ? /* @__PURE__ */ React.createElement("a", { href: a.pr_url, target: "_blank", rel: "noopener noreferrer" }, "#", a.pr_number || "") : "\u2014"),
        /* @__PURE__ */ React.createElement("td", null, formatAge(a.last_activity_at))
      )))));
    }
    function DetailRow({ label, value, mono, children }) {
      if (!children && (value === null || value === void 0 || value === "")) return null;
      return /* @__PURE__ */ React.createElement("div", { className: "oco-detail-row" }, /* @__PURE__ */ React.createElement("div", { className: "oco-detail-label" }, label), /* @__PURE__ */ React.createElement("div", { className: cn("oco-detail-value", mono && "oco-mono") }, children || value));
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
      const mergedAt = agent.pr_merged_at ? new Date(agent.pr_merged_at * 1e3).toLocaleString() : null;
      const doneAt = agent.done_at ? new Date(agent.done_at * 1e3).toLocaleString() : null;
      const createdAt = agent.created_at ? new Date(agent.created_at * 1e3).toLocaleString() : null;
      return /* @__PURE__ */ React.createElement(
        "div",
        {
          className: "oco-modal-backdrop",
          onClick: onClose,
          role: "presentation"
        },
        /* @__PURE__ */ React.createElement(
          "div",
          {
            className: "oco-modal",
            role: "dialog",
            "aria-modal": "true",
            "aria-label": "Agent " + agent.agent_id,
            onClick: (e) => e.stopPropagation()
          },
          /* @__PURE__ */ React.createElement("div", { className: "oco-modal-header" }, /* @__PURE__ */ React.createElement("div", { className: "oco-modal-title" }, /* @__PURE__ */ React.createElement("span", { className: "oco-mono" }, agent.agent_id), /* @__PURE__ */ React.createElement(PhaseCell, { phase: agent.phase })), /* @__PURE__ */ React.createElement(
            "button",
            {
              type: "button",
              className: "oco-modal-close",
              onClick: onClose,
              "aria-label": "close"
            },
            "\u2715"
          )),
          /* @__PURE__ */ React.createElement("div", { className: "oco-modal-body" }, /* @__PURE__ */ React.createElement(DetailRow, { label: "Project", value: agent.project_label }), /* @__PURE__ */ React.createElement(DetailRow, { label: "Branch", value: agent.branch, mono: true }), /* @__PURE__ */ React.createElement(DetailRow, { label: "Session", value: agent.session_id, mono: true }), /* @__PURE__ */ React.createElement(DetailRow, { label: "Worktree", value: agent.worktree_path, mono: true }), agent.reviewer_session_id && /* @__PURE__ */ React.createElement(
            DetailRow,
            {
              label: "Reviewer session",
              value: agent.reviewer_session_id,
              mono: true
            }
          ), agent.reviewer_worktree_path && /* @__PURE__ */ React.createElement(
            DetailRow,
            {
              label: "Reviewer worktree",
              value: agent.reviewer_worktree_path,
              mono: true
            }
          ), agent.review_cycle_count !== void 0 && /* @__PURE__ */ React.createElement(
            DetailRow,
            {
              label: "Review cycles",
              value: String(agent.review_cycle_count)
            }
          ), /* @__PURE__ */ React.createElement(DetailRow, { label: "Created", value: createdAt }), createdAt && /* @__PURE__ */ React.createElement(
            DetailRow,
            {
              label: "Age",
              value: formatAge(agent.created_at) + " ago"
            }
          ), /* @__PURE__ */ React.createElement(
            DetailRow,
            {
              label: "Last activity",
              value: formatAge(agent.last_activity_at) + " ago"
            }
          ), /* @__PURE__ */ React.createElement(DetailRow, { label: "PR" }, agent.pr_url ? /* @__PURE__ */ React.createElement(
            "a",
            {
              href: agent.pr_url,
              target: "_blank",
              rel: "noopener noreferrer"
            },
            agent.pr_url
          ) : "\u2014"), agent.pr_number && /* @__PURE__ */ React.createElement(DetailRow, { label: "PR number", value: "#" + agent.pr_number }), mergedAt && /* @__PURE__ */ React.createElement(DetailRow, { label: "Merged at", value: mergedAt }), doneAt && /* @__PURE__ */ React.createElement(DetailRow, { label: "Done at", value: doneAt }), agent.last_error && /* @__PURE__ */ React.createElement(DetailRow, { label: "Last error" }, /* @__PURE__ */ React.createElement("pre", { className: "oco-detail-error" }, agent.last_error)), /* @__PURE__ */ React.createElement(DetailRow, { label: "Initial prompt" }, /* @__PURE__ */ React.createElement("pre", { className: "oco-detail-prompt" }, agent.initial_prompt || "\u2014"))),
          /* @__PURE__ */ React.createElement("div", { className: "oco-modal-footer" }, /* @__PURE__ */ React.createElement("span", { className: "oco-modal-hint" }, "press Esc or click outside to close"))
        )
      );
    }
    function ProjectsTable({ projects }) {
      if (!projects || projects.length === 0) {
        return /* @__PURE__ */ React.createElement("div", { className: "oco-empty" }, "No projects registered.");
      }
      return /* @__PURE__ */ React.createElement("div", { className: "oco-table-wrap" }, /* @__PURE__ */ React.createElement("table", { className: "oco-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Label"), /* @__PURE__ */ React.createElement("th", null, "Abbrev"), /* @__PURE__ */ React.createElement("th", null, "Repo path"), /* @__PURE__ */ React.createElement("th", null, "Base branch"), /* @__PURE__ */ React.createElement("th", null, "Bootstrap skill"))), /* @__PURE__ */ React.createElement("tbody", null, projects.map((p) => /* @__PURE__ */ React.createElement("tr", { key: p.label }, /* @__PURE__ */ React.createElement("td", { className: "oco-mono" }, p.label), /* @__PURE__ */ React.createElement("td", { className: "oco-mono" }, p.abbrev), /* @__PURE__ */ React.createElement(
        "td",
        {
          className: cn(
            "oco-mono",
            "oco-truncate",
            !p.repo_exists && "oco-warn"
          ),
          title: p.repo_path
        },
        p.repo_path
      ), /* @__PURE__ */ React.createElement("td", { className: "oco-mono" }, p.base_branch), /* @__PURE__ */ React.createElement(
        "td",
        {
          className: "oco-mono oco-truncate",
          title: p.bootstrap_skill || ""
        },
        p.bootstrap_skill || "\u2014"
      ))))));
    }
    function HeartbeatsList({ items }) {
      if (!items || items.length === 0) {
        return /* @__PURE__ */ React.createElement("div", { className: "oco-empty" }, "No heartbeats yet.");
      }
      return /* @__PURE__ */ React.createElement("div", { className: "oco-heartbeats" }, items.map((h, i) => {
        const when = h.meta && h.meta.when ? h.meta.when : h.ts ? new Date(h.ts * 1e3).toLocaleString() : "";
        return /* @__PURE__ */ React.createElement("div", { key: i, className: "oco-heartbeat" }, /* @__PURE__ */ React.createElement("div", { className: "oco-heartbeat-when" }, when), /* @__PURE__ */ React.createElement("pre", { className: "oco-heartbeat-body" }, h.body || ""));
      }));
    }
    function RefreshButton({ onClick, refreshing, lastRefreshAt }) {
      const tooltip = lastRefreshAt ? "last refresh " + formatAge(lastRefreshAt) + " ago" : "refresh now";
      return /* @__PURE__ */ React.createElement(
        "button",
        {
          type: "button",
          className: cn("oco-refresh", refreshing && "oco-refresh-spinning"),
          onClick,
          disabled: refreshing,
          title: tooltip
        },
        /* @__PURE__ */ React.createElement("span", { className: "oco-refresh-icon", "aria-hidden": "true" }, "\u21BB"),
        refreshing ? "refreshing\u2026" : "refresh"
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
      const [transport, setTransport] = React.useState("poll");
      const [selectedAgentId, setSelectedAgentId] = React.useState(null);
      const [includeArchived, setIncludeArchived] = React.useState(false);
      const [archivedHidden, setArchivedHidden] = React.useState(0);
      const selectedAgent = React.useMemo(
        () => agents.find((a) => a.agent_id === selectedAgentId) || null,
        [agents, selectedAgentId]
      );
      const refresh = React.useCallback(
        async function() {
          setRefreshing(true);
          try {
            const agentsPath = includeArchived ? "/agents?include_archived=1" : "/agents";
            const [a, p, h] = await Promise.all([
              api(agentsPath),
              api("/projects"),
              api("/heartbeats?n=5")
            ]);
            setAgents(a && a.agents || []);
            setArchivedHidden(a && a.archived_hidden || 0);
            setProjects(p && p.projects || []);
            setHeartbeats(h && h.items || []);
            setError(null);
            setLastRefreshAt(Date.now() / 1e3);
          } catch (e) {
            setError(String(e));
          } finally {
            setLoading(false);
            setRefreshing(false);
          }
        },
        [includeArchived]
      );
      React.useEffect(
        function() {
          refresh();
          let pollId = null;
          let ws = null;
          let connectedAt = 0;
          let startedPolling = false;
          function startPolling() {
            if (startedPolling) return;
            startedPolling = true;
            setTransport("poll");
            pollId = setInterval(refresh, 5e3);
          }
          try {
            const token = window.__HERMES_SESSION_TOKEN__ || "";
            const proto = location.protocol === "https:" ? "wss:" : "ws:";
            const archivedQ = includeArchived ? "&include_archived=1" : "";
            const url = proto + "//" + location.host + "/api/plugins/hermes-opencode/events?token=" + encodeURIComponent(token) + archivedQ;
            ws = new WebSocket(url);
            ws.onopen = function() {
              connectedAt = Date.now();
              setTransport("ws");
            };
            ws.onmessage = function(ev) {
              try {
                const msg = JSON.parse(ev.data);
                if (!msg || !msg.type) return;
                if (msg.type === "snapshot") {
                  if (Array.isArray(msg.agents)) setAgents(msg.agents);
                  if (Array.isArray(msg.projects)) setProjects(msg.projects);
                  if (typeof msg.archived_hidden === "number") setArchivedHidden(msg.archived_hidden);
                  setLastRefreshAt(Date.now() / 1e3);
                } else if (msg.type === "agents") {
                  if (Array.isArray(msg.agents)) setAgents(msg.agents);
                  if (typeof msg.archived_hidden === "number") setArchivedHidden(msg.archived_hidden);
                  setLastRefreshAt(Date.now() / 1e3);
                } else if (msg.type === "heartbeat") {
                  if (Array.isArray(msg.items)) {
                    setHeartbeats(
                      (prev) => msg.items.concat(prev || []).slice(0, 5)
                    );
                  }
                  setLastRefreshAt(Date.now() / 1e3);
                }
              } catch (_) {
              }
            };
            ws.onerror = function() {
              if (Date.now() - connectedAt < 5e3) startPolling();
            };
            ws.onclose = function() {
              if (Date.now() - connectedAt < 5e3) startPolling();
            };
          } catch (_) {
            startPolling();
          }
          return function() {
            if (pollId !== null) clearInterval(pollId);
            if (ws) {
              try {
                ws.close();
              } catch (_) {
              }
            }
          };
        },
        [refresh, includeArchived]
      );
      return /* @__PURE__ */ React.createElement("div", { className: "oco-page" }, /* @__PURE__ */ React.createElement("header", { className: "oco-header" }, /* @__PURE__ */ React.createElement("div", { className: "oco-header-row" }, /* @__PURE__ */ React.createElement("h1", null, "Opencode Agents"), /* @__PURE__ */ React.createElement("div", { className: "oco-header-actions" }, /* @__PURE__ */ React.createElement("label", { className: "oco-archive-toggle" }, /* @__PURE__ */ React.createElement(
        "input",
        {
          type: "checkbox",
          checked: includeArchived,
          onChange: (e) => setIncludeArchived(e.target.checked)
        }
      ), /* @__PURE__ */ React.createElement("span", null, "show archived", archivedHidden > 0 && !includeArchived ? " (" + archivedHidden + " hidden)" : "")), /* @__PURE__ */ React.createElement(
        RefreshButton,
        {
          onClick: refresh,
          refreshing,
          lastRefreshAt
        }
      ))), /* @__PURE__ */ React.createElement("div", { className: "oco-stats" }, /* @__PURE__ */ React.createElement("span", null, agents.length, " agent", agents.length === 1 ? "" : "s"), /* @__PURE__ */ React.createElement("span", { className: "oco-sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", null, projects.length, " project", projects.length === 1 ? "" : "s"), /* @__PURE__ */ React.createElement("span", { className: "oco-sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "oco-transport oco-transport-" + transport }, transport), lastRefreshAt && /* @__PURE__ */ React.createElement("span", { className: "oco-sep" }, "\xB7"), lastRefreshAt && /* @__PURE__ */ React.createElement("span", { className: "oco-loading" }, "updated ", formatAge(lastRefreshAt), " ago")), error && /* @__PURE__ */ React.createElement("div", { className: "oco-error" }, error)), /* @__PURE__ */ React.createElement("section", { className: "oco-section" }, /* @__PURE__ */ React.createElement("h2", null, "Agents"), /* @__PURE__ */ React.createElement(AgentsTable, { agents, onSelect: setSelectedAgentId })), /* @__PURE__ */ React.createElement("section", { className: "oco-section" }, /* @__PURE__ */ React.createElement("h2", null, "Projects"), /* @__PURE__ */ React.createElement(ProjectsTable, { projects })), /* @__PURE__ */ React.createElement("section", { className: "oco-section" }, /* @__PURE__ */ React.createElement("h2", null, "Recent heartbeats"), /* @__PURE__ */ React.createElement(HeartbeatsList, { items: heartbeats })), selectedAgent && /* @__PURE__ */ React.createElement(
        AgentDetailModal,
        {
          agent: selectedAgent,
          onClose: () => setSelectedAgentId(null)
        }
      ));
    }
    window.__HERMES_PLUGINS__.register("hermes-opencode", OpencodeAgentsPage);
  })();
})();
