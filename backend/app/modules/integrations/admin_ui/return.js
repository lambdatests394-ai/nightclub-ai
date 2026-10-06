"use strict";
(() => {
  const query = new URLSearchParams(window.location.search);
  // Remove code/state before styles load; the initial HTTP request still needs log redaction.
  window.history.replaceState(null, "", window.location.pathname);
  const value = (name, limit) => {
    const all = query.getAll(name);
    return all.length === 1 && all[0].length > 0 && all[0].length <= limit &&
      all[0] === all[0].trim() && !/[\x00-\x1f\x7f-\x9f]/.test(all[0]) ? all[0] : null;
  };
  const state = value("state", 4096);
  const code = value("code", 2048);
  const cancelled = query.getAll("error").length === 1 && !query.has("code");
  if (!state || (!code && !cancelled) || (code && query.has("error")) || !window.opener) return;
  window.opener.postMessage({type: "nightclub-facebook-return", state,
    code: cancelled ? null : code, cancelled}, window.location.origin);
})();
