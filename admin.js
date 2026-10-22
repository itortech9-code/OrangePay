const adminStatus = document.querySelector("#admin-status");

function setAdminStatus(message, isError = false) {
  adminStatus.textContent = message;
  adminStatus.classList.toggle("error", isError);
}

async function readAdminResponse(response) {
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("The server returned an invalid response. Please try again.");
  }
  if (!response.ok) throw new Error(result.error || "Could not complete the request.");
  return result;
}

async function loadAdminUsers(query = "") {
  setAdminStatus("Loading accounts…");
  try {
    const result = await readAdminResponse(await apiFetch(
      `/api/admin/users${query ? `?q=${encodeURIComponent(query)}` : ""}`,
    ));
    const tableBody = document.querySelector("#user-results");
    tableBody.replaceChildren();
    result.users.forEach((user) => {
      const row = document.createElement("tr");
      const contact = user.email || user.phone || "—";
      const values = [
        user.name,
        contact,
        user.status,
        user.kyc_status || "not_started",
        user.created_at ? new Date(user.created_at).toLocaleDateString() : "—",
      ];
      values.forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      tableBody.append(row);
    });
    setAdminStatus(`${result.users.length} account${result.users.length === 1 ? "" : "s"} found (maximum 50).`);
  } catch (error) {
    setAdminStatus(error instanceof Error ? error.message : "Could not load accounts.", true);
  }
}

if (document.body.dataset.adminPage === "setup") {
  apiFetch("/api/admin/setup-status").then(async (response) => {
    const result = await readAdminResponse(response);
    setAdminStatus(result.available
      ? "One-time setup is available for the first administrator."
      : "One-time administrator setup is unavailable or has already been claimed.", !result.available);
    document.querySelector("#admin-setup-form button").disabled = !result.available;
  }).catch((error) => {
    setAdminStatus(error instanceof Error ? error.message : "Could not check administrator setup.", true);
  });

  document.querySelector("#admin-setup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    const tokenInput = form.elements.token;
    button.disabled = true;
    setAdminStatus("Enabling administrator access…");
    try {
      await readAdminResponse(await apiFetch("/api/admin/claim", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tokenInput.value }),
      }));
      tokenInput.value = "";
      window.location.replace("/admin");
    } catch (error) {
      setAdminStatus(error instanceof Error ? error.message : "Could not enable administrator access.", true);
      button.disabled = false;
    }
  });
} else if (document.body.dataset.adminPage === "support") {
  document.querySelector("#user-search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadAdminUsers(event.currentTarget.elements.q.value.trim());
  });
  setAdminStatus("Search for a user to view account-support details.");
}
