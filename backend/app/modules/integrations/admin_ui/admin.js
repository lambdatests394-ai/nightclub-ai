"use strict";
(() => {
  const el = id => document.getElementById(id);
  let config = null, token = null, pending = null, busy = false;
  const say = text => { el("status").textContent = text; };
  const controls = () => {
    el("login-button").disabled = busy || !config;
    el("connect-button").disabled = busy || !token;
    el("organization").disabled = busy; el("page").disabled = busy;
  };
  const finish = () => {
    if (pending) { clearTimeout(pending.timer); pending.popup.close(); }
    pending = null; busy = false; controls();
  };
  const forget = () => {
    token = null; finish(); el("password").value = "";
    el("login").hidden = false; el("connection").hidden = true;
  };
  const api = async (path, body, organization, extra = {}) => {
    const response = await fetch(path, {method: "POST", credentials: "omit", cache: "no-store",
      redirect: "error", signal: AbortSignal.timeout(30000),
      headers: {"Content-Type": "application/json", Authorization: "Bearer " + token,
        "X-Organization-ID": organization, ...extra}, body: JSON.stringify(body)});
    if (!response.ok) {
      if (response.status === 401) { forget(); throw new Error("session"); }
      throw new Error("request");
    }
    return (await response.json()).data;
  };
  el("login").addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !config) return;
    busy = true; controls();
    const password = el("password").value; el("password").value = "";
    try {
      const response = await fetch(config.authUrl + "/token?grant_type=password", {
        method: "POST", credentials: "omit", cache: "no-store", redirect: "error",
        signal: AbortSignal.timeout(30000), headers: {"Content-Type": "application/json", apikey: config.publishableKey},
        body: JSON.stringify({email: el("email").value.trim(), password})});
      if (!response.ok) throw new Error("login");
      const data = await response.json();
      if (typeof data.access_token !== "string" || !data.access_token || data.access_token.length > 16384) throw new Error("login");
      // This small screen has no MFA challenge flow; do not proceed at AAL1 for MFA accounts.
      if (Array.isArray(data.user?.factors) && data.user.factors.some(factor => factor.status === "verified")) throw new Error("mfa");
      token = data.access_token; // No refresh token, cookies or browser storage.
      el("login").hidden = true; el("connection").hidden = false;
      say("Selecciona la organización y la página que deseas conectar.");
    } catch (error) {
      token = null;
      say(error.message === "mfa" ? "Esta pantalla aún no admite cuentas con verificación en dos pasos." : "No se pudo iniciar sesión. Revisa tu acceso administrativo.");
    }
    finally { busy = false; controls(); }
  });
  el("logout").addEventListener("click", () => { forget(); say("Acceso cerrado en esta pantalla."); });
  el("connection").addEventListener("submit", async event => {
    event.preventDefault(); if (busy || !token) return;
    const organization = el("organization").value.trim(), page = el("page").value.trim();
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(organization) || !/^[1-9][0-9]{0,127}$/.test(page)) {
      say("Revisa el ID de organización y el ID de página."); return;
    }
    const popup = window.open("about:blank", "", "popup,width=620,height=760");
    if (!popup) { say("Permite la ventana auxiliar e inténtalo de nuevo."); return; }
    busy = true; controls();
    // Capture the session and organization, so logout cannot complete an old request.
    const session = token;
    pending = {popup, organization, page, session, state: null, timer: null};
    const attempt = pending;
    say("Preparando la autorización de Facebook…");
    try {
      const data = await api("/api/v1/connections/facebook/oauth/start", {requestedPageId: page}, organization,
        {"Idempotency-Key": crypto.randomUUID()});
      if (pending !== attempt || token !== session) return;
      const url = new URL(data.authorizationUrl), redirect = url.searchParams.getAll("redirect_uri"), states = url.searchParams.getAll("state");
      const expires = Date.parse(data.expiresAt), remaining = expires - Date.now();
      if (url.origin !== "https://www.facebook.com" || url.username || url.password || url.hash ||
          !/^\/v[1-9][0-9]*\.[0-9]+\/dialog\/oauth$/.test(url.pathname) ||
          redirect.length !== 1 || redirect[0] !== window.location.origin + "/admin/facebook/return" ||
          states.length !== 1 || !states[0] || states[0].length > 4096 || data.requestedPageId !== page ||
          !Number.isFinite(remaining) || remaining <= 0 || remaining > 600000) throw new Error("config");
      pending.state = states[0];
      pending.timer = setTimeout(() => { finish(); say("La autorización venció. Inicia nuevamente."); }, remaining);
      popup.location.replace(url.href);
      say("Completa el consentimiento en la ventana de Facebook.");
    } catch (error) {
      if (pending === attempt) finish();
      say(error.message === "session" ? "La sesión venció. Inicia sesión y repite la conexión." : "No se pudo iniciar la conexión. Revisa la configuración o tus permisos.");
    }
  });
  window.addEventListener("message", async event => {
    const attempt = pending, data = event.data;
    if (!attempt || !attempt.state || event.origin !== window.location.origin || event.source !== attempt.popup ||
        !data || data.type !== "nightclub-facebook-return" || data.state !== attempt.state) return;
    if (data.cancelled === true && data.code === null) { finish(); say("Autorización cancelada. No se guardó una conexión."); return; }
    if (data.cancelled !== false || typeof data.code !== "string" || !data.code || data.code.length > 2048 ||
        data.code !== data.code.trim() || /[\x00-\x1f\x7f-\x9f]/.test(data.code)) return;
    const organization = attempt.organization, state = attempt.state, session = attempt.session;
    finish(); busy = true; controls(); // Accept at most one browser message.
    try {
      if (!token || token !== session) throw new Error("session");
      const connection = await api("/api/v1/connections/facebook/oauth/complete", {state, code: data.code}, organization);
      if (token !== session) return;
      say("Conexión guardada para la página " + connection.externalAccountId + ".");
    } catch (error) {
      say(error.message === "session" ? "La sesión venció. Inicia sesión y repite la conexión." : "No se pudo completar la conexión. Inicia una autorización nueva; no repitas este retorno.");
    } finally { busy = false; controls(); }
  });
  window.addEventListener("pagehide", forget);
  controls();
  fetch("/admin/facebook/config", {credentials: "omit", cache: "no-store", redirect: "error", signal: AbortSignal.timeout(10000)})
    .then(async response => { if (!response.ok) throw new Error("config"); return response.json(); })
    .then(data => { config = data; controls(); say("Inicia sesión para conectar tu página."); })
    .catch(() => say("El acceso administrativo no está configurado."));
})();
