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
      KILLED: "\u{1F6D1}"
    };
    async function api(path, options) {
      const token = window.__HERMES_SESSION_TOKEN__ || "";
      const headers = Object.assign({}, options && options.headers || {});
      if (token) headers["X-Hermes-Session-Token"] = token;
      const res = await fetch("/api/plugins/opencode-orchestrator" + path, {
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
    function AgentsTable({ agents }) {
      if (!agents || agents.length === 0) {
        return /* @__PURE__ */ React.createElement("div", { className: "oco-empty" }, "No agents tracked.");
      }
      return /* @__PURE__ */ React.createElement("div", { className: "oco-table-wrap" }, /* @__PURE__ */ React.createElement("table", { className: "oco-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Agent"), /* @__PURE__ */ React.createElement("th", null, "Project"), /* @__PURE__ */ React.createElement("th", null, "Branch"), /* @__PURE__ */ React.createElement("th", null, "Phase"), /* @__PURE__ */ React.createElement("th", null, "PR"), /* @__PURE__ */ React.createElement("th", null, "Last activity"))), /* @__PURE__ */ React.createElement("tbody", null, agents.map((a) => /* @__PURE__ */ React.createElement("tr", { key: a.agent_id }, /* @__PURE__ */ React.createElement("td", { className: "oco-mono" }, a.agent_id), /* @__PURE__ */ React.createElement("td", null, a.project_label), /* @__PURE__ */ React.createElement("td", { className: "oco-mono" }, a.branch), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement(PhaseCell, { phase: a.phase })), /* @__PURE__ */ React.createElement("td", null, a.pr_url ? /* @__PURE__ */ React.createElement("a", { href: a.pr_url, target: "_blank", rel: "noopener noreferrer" }, "#", a.pr_number || "") : "\u2014"), /* @__PURE__ */ React.createElement("td", null, formatAge(a.last_activity_at)))))));
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
      const refresh = React.useCallback(async function() {
        setRefreshing(true);
        try {
          const [a, p, h] = await Promise.all([
            api("/agents"),
            api("/projects"),
            api("/heartbeats?n=5")
          ]);
          setAgents(a && a.agents || []);
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
      }, []);
      React.useEffect(
        function() {
          refresh();
          const id = setInterval(refresh, 5e3);
          return function() {
            clearInterval(id);
          };
        },
        [refresh]
      );
      return /* @__PURE__ */ React.createElement("div", { className: "oco-page" }, /* @__PURE__ */ React.createElement("header", { className: "oco-header" }, /* @__PURE__ */ React.createElement("div", { className: "oco-header-row" }, /* @__PURE__ */ React.createElement("h1", null, "Opencode Agents"), /* @__PURE__ */ React.createElement(
        RefreshButton,
        {
          onClick: refresh,
          refreshing,
          lastRefreshAt
        }
      )), /* @__PURE__ */ React.createElement("div", { className: "oco-stats" }, /* @__PURE__ */ React.createElement("span", null, agents.length, " agent", agents.length === 1 ? "" : "s"), /* @__PURE__ */ React.createElement("span", { className: "oco-sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", null, projects.length, " project", projects.length === 1 ? "" : "s"), lastRefreshAt && /* @__PURE__ */ React.createElement("span", { className: "oco-sep" }, "\xB7"), lastRefreshAt && /* @__PURE__ */ React.createElement("span", { className: "oco-loading" }, "updated ", formatAge(lastRefreshAt), " ago")), error && /* @__PURE__ */ React.createElement("div", { className: "oco-error" }, error)), /* @__PURE__ */ React.createElement("section", { className: "oco-section" }, /* @__PURE__ */ React.createElement("h2", null, "Agents"), /* @__PURE__ */ React.createElement(AgentsTable, { agents })), /* @__PURE__ */ React.createElement("section", { className: "oco-section" }, /* @__PURE__ */ React.createElement("h2", null, "Projects"), /* @__PURE__ */ React.createElement(ProjectsTable, { projects })), /* @__PURE__ */ React.createElement("section", { className: "oco-section" }, /* @__PURE__ */ React.createElement("h2", null, "Recent heartbeats"), /* @__PURE__ */ React.createElement(HeartbeatsList, { items: heartbeats })));
    }
    window.__HERMES_PLUGINS__.register("opencode-orchestrator", OpencodeAgentsPage);
  })();
})();
