async function req(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (opts.json !== undefined) {
    init.method = opts.method || "POST";
    init.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    init.body = JSON.stringify(opts.json);
    delete init.json;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    const err = new Error("Backend not reachable — is the server running?");
    err.status = 0;
    throw err;
  }
  if (res.status === 401 && !path.startsWith("/api/auth/")) window.dispatchEvent(new Event("rx:unauthorized"));
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { msg = (await res.json()).detail || msg; } catch {
      if (res.status >= 500) msg += " — check the backend terminal/log (backend/data/backend.log) for the traceback";
    }
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export const api = {
  health: () => req("/api/health"),
  // auth
  authConfig: () => req("/api/auth/config"),
  me: () => req("/api/auth/me"),
  login: (username, password) => req("/api/auth/login", { json: { username, password } }),
  signup: (username, password) => req("/api/auth/signup", { json: { username, password } }),
  guest: () => req("/api/auth/guest", { method: "POST" }),
  logout: () => req("/api/auth/logout", { method: "POST" }),
  upgrade: (username, password) => req("/api/auth/upgrade", { json: { username, password } }),
  changePassword: (current, next) => req("/api/auth/password", { json: { current, new: next } }),
  myUsage: () => req("/api/me/usage"),
  estimates: () => req("/api/estimates"),
  // runs
  runs: (all = false) => req(`/api/runs${all ? "?all=true" : ""}`),
  run: (id) => req(`/api/runs/${id}`),
  create: (form) => req("/api/runs", { method: "POST", body: form }),
  resume: (id) => req(`/api/runs/${id}/resume`, { method: "POST" }),
  cancel: (id) => req(`/api/runs/${id}/cancel`, { method: "POST" }),
  remove: (id) => req(`/api/runs/${id}`, { method: "DELETE" }),
  evaluate: (id) => req(`/api/runs/${id}/evaluate`, { method: "POST" }),
  ask: (id, question) => req(`/api/runs/${id}/ask`, { json: { question } }),
  events: (id, after = 0) => new EventSource(`/api/runs/${id}/events?after=${after}`),
  // evaluations
  evals: (all = false) => req(`/api/evals${all ? "?all=true" : ""}`),
  // admin
  overview: () => req("/api/admin/overview"),
  users: () => req("/api/admin/users"),
  createUser: (body) => req("/api/admin/users", { json: body }),
  updateUser: (id, patch) => req(`/api/admin/users/${id}`, { method: "PATCH", json: patch }),
  deleteUser: (id) => req(`/api/admin/users/${id}`, { method: "DELETE" }),
  saveSettings: (patch) => req("/api/admin/settings", { method: "PUT", json: patch }),
  purgeGuests: () => req("/api/admin/guests/purge", { method: "POST" }),
};
