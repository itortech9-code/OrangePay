let csrfToken;
let csrfTokenPromise;

async function getCsrfToken() {
  if (csrfToken) return csrfToken;
  if (!csrfTokenPromise) {
    csrfTokenPromise = fetch("/api/auth/csrf", { credentials: "same-origin" })
      .then(async (response) => {
        const result = await response.json();
        if (!response.ok || typeof result.csrf_token !== "string") {
          throw new Error(result.error || "Could not initialize request security.");
        }
        csrfToken = result.csrf_token;
        return csrfToken;
      })
      .finally(() => {
        csrfTokenPromise = undefined;
      });
  }
  return csrfTokenPromise;
}

async function apiFetch(resource, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
    headers.set("X-CSRFToken", await getCsrfToken());
  }
  return fetch(resource, {
    ...options,
    headers,
    credentials: "same-origin",
  });
}

window.apiFetch = apiFetch;
