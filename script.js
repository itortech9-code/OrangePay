const currencySymbols = { USD: "$", EUR: "€", GBP: "£", NGN: "₦" };
const accountClasses = { USD: "flag-us", EUR: "flag-eu", GBP: "flag-gb" };
const recipientClasses = ["recipient-blue", "recipient-peach", "recipient-green"];
const balanceAmount = document.querySelector("#main-balance");
const dialog = document.querySelector("#transfer-dialog");
const recipientInput = document.querySelector("#recipient-name");
const toast = document.querySelector("#toast");
let balanceVisible = true;
let toastTimeout;
let activeTransactionFilter = "all";
let totalBalanceMinor = null;
let exchangeRequestId = 0;
let exchangeTimeout;
let transferOtpReference = "";
let crispWebsiteId = "";
let smileReady = false;

const currentHour = new Date().getHours();
const greeting = currentHour < 12 ? "Good morning" : currentHour < 18 ? "Good afternoon" : "Good evening";
document.querySelector(".welcome-row h1").firstChild.textContent = `${greeting}, Jordan `;
document.querySelector("#today-label").textContent = `${new Intl.DateTimeFormat("en", {
  weekday: "long",
  month: "short",
  day: "numeric",
}).format(new Date()).toUpperCase()} · YOUR ACCOUNT OVERVIEW`;

function formatAmount(amount, currency) {
  return `${currencySymbols[currency]}${Number(amount).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatMinorAmount(amountMinor, currency) {
  return formatAmount(amountMinor / 100, currency);
}

function updateBalanceDisplay() {
  balanceAmount.textContent = !balanceVisible
    ? "••••••••"
    : totalBalanceMinor === null ? "Loading…" : formatMinorAmount(totalBalanceMinor, "USD");
}

async function updateExchange() {
  const amount = Number(document.querySelector("#exchange-amount").value);
  const from = document.querySelector("#from-currency").value;
  const to = document.querySelector("#to-currency").value;
  const requestId = ++exchangeRequestId;
  const rateCopy = document.querySelector("#rate-copy");
  const resultOutput = document.querySelector("#exchange-result");
  const provider = document.querySelector("#fx-updated");
  if (!Number.isFinite(amount) || amount <= 0) {
    rateCopy.textContent = "Enter an amount to get a quote";
    resultOutput.textContent = "—";
    provider.textContent = "Waiting for amount";
    return;
  }

  rateCopy.textContent = "Fetching indicative quote…";
  provider.textContent = "Fetching";
  try {
    const params = new URLSearchParams({ from, to, amount: String(amount) });
    const response = await apiFetch(`/api/fx/quote?${params}`);
    const quote = await response.json();
    if (!response.ok) throw new Error(quote.error || "Quote unavailable.");
    if (requestId !== exchangeRequestId) return;
    const formattedRate = Number(quote.rate).toLocaleString("en-US", { maximumSignificantDigits: 7 });
    rateCopy.textContent = `1 ${from} = ${formattedRate} ${to}`;
    resultOutput.textContent = formatAmount(quote.result, to);
    provider.textContent = quote.updated_at
      ? `Updated ${new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(new Date(quote.updated_at))}`
      : "Reference rate";
  } catch (error) {
    if (requestId !== exchangeRequestId) return;
    rateCopy.textContent = error instanceof Error ? error.message : "Quote unavailable.";
    resultOutput.textContent = "Unavailable";
    provider.textContent = "Not connected";
  }
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(toastTimeout);
  toastTimeout = window.setTimeout(() => toast.classList.remove("visible"), 2800);
}

function transactionDate(isoDate) {
  const date = new Date(isoDate);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  const sameDay = (left, right) => left.toDateString() === right.toDateString();
  const day = sameDay(date, today) ? "Today" : sameDay(date, yesterday) ? "Yesterday" : new Intl.DateTimeFormat("en", { month: "short", day: "numeric" }).format(date);
  const time = new Intl.DateTimeFormat("en", { hour: "numeric", minute: "2-digit" }).format(date);
  return `${day}, ${time}`;
}

function renderTransaction(transaction, prepend = false) {
  const row = document.createElement("article");
  row.className = "transaction-row";
  row.dataset.kind = transaction.kind;

  const icon = document.createElement("span");
  const isTransfer = transaction.description.startsWith("Transfer to ");
  icon.className = `transaction-icon ${isTransfer ? "icon-transfer" : "icon-shop"}`;
  icon.textContent = isTransfer ? "↗" : transaction.description.charAt(0).toUpperCase();

  const copy = document.createElement("span");
  copy.className = "transaction-copy";
  const title = document.createElement("strong");
  title.textContent = transaction.description;
  const details = document.createElement("small");
  details.textContent = `${transactionDate(transaction.created_at)} · ${transaction.account_currency} account`;
  copy.append(title, details);

  const value = document.createElement("span");
  value.className = `transaction-amount${transaction.kind === "income" ? " positive" : ""}`;
  value.append(document.createTextNode(`${transaction.kind === "income" ? "+" : "−"}${formatMinorAmount(transaction.amount_minor, transaction.currency)}`));
  const status = document.createElement("small");
  status.textContent = transaction.status;
  value.append(status);
  row.append(icon, copy, value);

  const list = document.querySelector("#transaction-list");
  if (prepend) list.prepend(row);
  else list.append(row);
  applyTransactionFilter();
}

function applyTransactionFilter() {
  let visibleCount = 0;
  document.querySelectorAll(".transaction-row").forEach((row) => {
    row.hidden = activeTransactionFilter !== "all" && row.dataset.kind !== activeTransactionFilter;
    if (!row.hidden) visibleCount += 1;
  });
  document.querySelector("#empty-transactions").hidden = visibleCount !== 0;
}

function renderAccount(account) {
  const row = document.createElement("article");
  row.className = "account-row";
  const flag = document.createElement("span");
  flag.className = `currency-flag ${accountClasses[account.currency] ?? ""}`;
  flag.textContent = account.currency.slice(0, 2);

  const info = document.createElement("div");
  info.className = "account-info";
  const accountName = document.createElement("strong");
  accountName.append(document.createTextNode(account.name));
  const currency = document.createElement("span");
  currency.className = "currency-code";
  currency.textContent = account.currency;
  accountName.append(currency);
  const maskedNumber = document.createElement("small");
  maskedNumber.textContent = account.masked_number;
  info.append(accountName, maskedNumber);

  const value = document.createElement("div");
  value.className = "account-value";
  const localBalance = document.createElement("strong");
  localBalance.textContent = formatMinorAmount(account.balance_minor, account.currency);
  const usdBalance = document.createElement("small");
  usdBalance.textContent = `≈ ${formatMinorAmount(account.usd_equivalent_minor, "USD")} USD`;
  value.append(localBalance, usdBalance);
  row.append(flag, info, value);
  document.querySelector("#account-list").append(row);
}

function renderBankLink(link) {
  const row = document.createElement("article");
  row.className = "account-row";
  const icon = document.createElement("span");
  icon.className = "currency-flag";
  icon.textContent = link.currency || "BK";

  const info = document.createElement("div");
  info.className = "account-info";
  const institution = document.createElement("strong");
  institution.textContent = link.institution || "External bank";
  const number = document.createElement("small");
  number.textContent = link.masked_number || "Account details pending";
  info.append(institution, number);

  const value = document.createElement("div");
  value.className = "account-value";
  const status = document.createElement("strong");
  status.className = "linked-bank-status";
  status.textContent = link.status || "pending";
  const note = document.createElement("small");
  note.textContent = "Not included in demo balance";
  value.append(status, note);
  row.append(icon, info, value);
  document.querySelector("#linked-bank-list").append(row);
}

function renderRecipient(name, index) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "recipient";
  button.dataset.recipient = name;
  const avatar = document.createElement("span");
  avatar.className = `recipient-avatar ${recipientClasses[index % recipientClasses.length]}`;
  avatar.textContent = name.split(/\s+/).slice(0, 2).map((part) => part.charAt(0).toUpperCase()).join("");
  const label = document.createElement("span");
  label.textContent = name.split(/\s+/)[0];
  button.append(avatar, label);
  button.addEventListener("click", () => openTransfer(name));
  document.querySelector("#recipients-list").append(button);
}

function renderNewRecipientButton() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "recipient";
  button.dataset.openTransfer = "";
  const avatar = document.createElement("span");
  avatar.className = "recipient-avatar recipient-add";
  avatar.textContent = "＋";
  const label = document.createElement("span");
  label.textContent = "New";
  button.append(avatar, label);
  button.addEventListener("click", () => openTransfer());
  document.querySelector("#recipients-list").append(button);
}

function setApiStatus(message, isError = false) {
  const status = document.querySelector("#api-status");
  status.textContent = message;
  status.classList.toggle("error", isError);
  status.hidden = !isError;
}

async function loadDashboard() {
  try {
    const response = await apiFetch("/api/dashboard");
    const data = await response.json();
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error(data.error || "Could not load the account.");

    const totalMinor = data.accounts.reduce((total, account) => total + account.usd_equivalent_minor, 0);
    totalBalanceMinor = totalMinor;
    updateBalanceDisplay();
    if (data.accounts.length === 0) balanceAmount.textContent = "—";
    document.querySelector("#account-currency-count").textContent = `${data.accounts.length} currencies`;
    document.querySelector("#empty-accounts").hidden = data.accounts.length !== 0;

    const usdTransactions = data.transactions.filter((transaction) => transaction.currency === "USD");
    const incomingTransactions = usdTransactions.filter((transaction) => transaction.kind === "income");
    const outgoingTransactions = usdTransactions.filter((transaction) => transaction.kind === "expense");
    const moneyIn = incomingTransactions.reduce((total, transaction) => total + transaction.amount_minor, 0);
    const moneyOut = outgoingTransactions.reduce((total, transaction) => total + transaction.amount_minor, 0);
    document.querySelector("#money-in-total").textContent = formatMinorAmount(moneyIn, "USD");
    document.querySelector("#money-out-total").textContent = formatMinorAmount(moneyOut, "USD");
    document.querySelector("#money-in-count").textContent = `${incomingTransactions.length} recent USD record${incomingTransactions.length === 1 ? "" : "s"}`;
    document.querySelector("#money-out-count").textContent = `${outgoingTransactions.length} recent USD record${outgoingTransactions.length === 1 ? "" : "s"}`;
    document.querySelector("#account-list").replaceChildren();
    data.accounts.forEach(renderAccount);
    document.querySelector("#linked-bank-list").replaceChildren();
    (data.bank_links || []).forEach(renderBankLink);
    document.querySelector("#linked-banks").hidden = !data.bank_links?.length;
    document.querySelector("#transaction-list").replaceChildren();
    data.transactions.forEach((transaction) => renderTransaction(transaction));
    document.querySelector("#recipients-list").replaceChildren();
    data.recipients.forEach(renderRecipient);
    renderNewRecipientButton();
    setApiStatus("");
  } catch (error) {
    setApiStatus(error instanceof Error ? error.message : "Could not connect to the account database.", true);
  }
}

function openTransfer(recipient = "") {
  document.querySelector("#transfer-form").reset();
  document.querySelector("#dialog-status").textContent = "";
  document.querySelector("#dialog-status").classList.remove("error");
  document.querySelector("#otp-label").hidden = true;
  transferOtpReference = "";
  recipientInput.readOnly = false;
  recipientInput.value = recipient;
  dialog.showModal();
  if (!document.querySelector("#transfer-bank").options.length || document.querySelector("#transfer-bank").options.length === 1) {
    loadBanks();
  }
  (recipient ? document.querySelector("#transfer-account") : recipientInput).focus();
}

async function loadBanks() {
  const select = document.querySelector("#transfer-bank");
  try {
    const response = await apiFetch("/api/payments/banks");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load the bank directory.");
    select.replaceChildren(new Option("Select a bank", ""));
    data.banks.forEach((bank) => select.add(new Option(bank.name, bank.code)));
  } catch (error) {
    select.replaceChildren(new Option("Bank list unavailable", ""));
    document.querySelector("#dialog-status").textContent = error instanceof Error ? error.message : "Could not load banks.";
    document.querySelector("#dialog-status").classList.add("error");
  }
}

async function loadIntegrationStatus() {
  try {
    const response = await apiFetch("/api/integrations/status");
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || "Could not check integration status.");
    crispWebsiteId = status.crisp_website_id || "";
    smileReady = status.identity_verification === "ready";
    document.querySelector("#start-kyc").disabled = !smileReady;
    document.querySelector("#kyc-privacy-link").href = status.privacy_url || "#";
    const paystackReady = status.paystack === "sandbox configured";
    const readyProviders = [
      paystackReady && "Paystack sandbox",
      status.bank_linking === "ready" && "Mono bank linking",
      status.support === "ready" && "Crisp support",
    ].filter(Boolean);
    document.querySelector("#integration-heading").textContent = readyProviders.length
      ? `${readyProviders.join(" · ")} ready`
      : "Provider setup needed";
    document.querySelector("#integration-copy").textContent = [
      paystackReady ? "NGN checkout and payouts use test mode." : "Paystack test key missing.",
      status.bank_linking === "ready" ? "Mono account linking is enabled." : "Mono keys and webhook secret are needed.",
      status.identity_verification === "ready" ? "Smile ID sandbox verification is enabled; results are confirmed by webhook." : "Smile ID verification is not configured.",
      status.email_notifications === "credentials configured" ? "Resend credentials are present." : "Resend email is not configured.",
      status.sms_notifications === "credentials configured" ? "Termii credentials are present." : "Termii SMS is not configured.",
      status.support === "ready" ? "Click Get support to open Crisp chat." : "Crisp support is not configured.",
    ].join(" ");
  } catch (error) {
    document.querySelector("#integration-heading").textContent = "Integration status unavailable";
    document.querySelector("#integration-copy").textContent = error instanceof Error ? error.message : "Could not check integrations.";
  }
}

document.querySelectorAll("[data-open-transfer]").forEach((button) => {
  button.addEventListener("click", () => openTransfer());
});

document.querySelector("#close-dialog").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

document.querySelector("#transfer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const transferForm = event.currentTarget;
  const form = new FormData(transferForm);
  const status = document.querySelector("#dialog-status");
  const button = transferForm.querySelector('[type="submit"]');
  button.disabled = true;
  status.textContent = transferOtpReference ? "Submitting OTP to Paystack…" : "Verifying account and submitting to Paystack…";
  status.classList.remove("error");

  try {
    const endpoint = transferOtpReference ? "/api/transfers/finalize" : "/api/transfers";
    const body = transferOtpReference
      ? { reference: transferOtpReference, otp: form.get("otp") }
      : {
        recipient: form.get("recipient"),
        account_number: form.get("account_number"),
        bank_code: form.get("bank_code"),
        amount: form.get("amount"),
        reason: form.get("reason"),
      };
    const response = await apiFetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) {
      if (result.verified_account_name) {
        recipientInput.value = result.verified_account_name;
        recipientInput.readOnly = true;
      }
      throw new Error(result.error || "Paystack could not process the transfer.");
    }
    if (result.status === "otp") {
      transferOtpReference = result.reference;
      document.querySelector("#otp-label").hidden = false;
      document.querySelector("#transfer-submit").textContent = "Finalize transfer →";
      status.textContent = result.message;
      document.querySelector("#transfer-otp").focus();
    } else {
      transferOtpReference = "";
      document.querySelector("#otp-label").hidden = true;
      status.textContent = result.message;
      showToast("Transfer submitted to Paystack sandbox.");
      await loadDashboard();
    }
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : "Could not save this demo payment.";
    status.classList.add("error");
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#verify-account").addEventListener("click", async () => {
  const accountNumber = document.querySelector("#transfer-account").value.trim();
  const bankCode = document.querySelector("#transfer-bank").value;
  const status = document.querySelector("#dialog-status");
  if (!/^\d{10}$/.test(accountNumber) || !bankCode) {
    status.textContent = "Select a bank and enter the 10-digit account number first.";
    status.classList.add("error");
    return;
  }
  status.textContent = "Checking account name with Paystack…";
  status.classList.remove("error");
  try {
    const response = await apiFetch("/api/transfers/resolve-account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_number: accountNumber, bank_code: bankCode }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not verify the account.");
    recipientInput.value = result.account_name;
    recipientInput.readOnly = true;
    status.textContent = `Verified account: ${result.account_name}`;
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : "Could not verify the account.";
    status.classList.add("error");
  }
});

["#transfer-account", "#transfer-bank"].forEach((selector) => {
  document.querySelector(selector).addEventListener("input", () => { recipientInput.readOnly = false; });
  document.querySelector(selector).addEventListener("change", () => { recipientInput.readOnly = false; });
});

document.querySelectorAll("[data-open-payment]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelector("#payment-status").textContent = "";
    document.querySelector("#payment-status").classList.remove("error");
    document.querySelector("#payment-dialog").showModal();
  });
});

const paymentDialog = document.querySelector("#payment-dialog");
document.querySelector("#close-payment-dialog").addEventListener("click", () => paymentDialog.close());
paymentDialog.addEventListener("click", (event) => {
  if (event.target === paymentDialog) paymentDialog.close();
});

document.querySelector("#payment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const button = document.querySelector("#payment-submit");
  const status = document.querySelector("#payment-status");
  button.disabled = true;
  status.textContent = "Creating secure Paystack checkout…";
  status.classList.remove("error");
  try {
    const response = await apiFetch("/api/payments/initialize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.get("email"), amount: form.get("amount") }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not create checkout.");
    const checkout = new URL(result.authorization_url);
    if (checkout.protocol !== "https:" || checkout.hostname !== "checkout.paystack.com") {
      throw new Error("Paystack returned an unexpected checkout address.");
    }
    window.location.assign(checkout.href);
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : "Could not create checkout.";
    status.classList.add("error");
    button.disabled = false;
  }
});

document.querySelector("#toggle-balance").addEventListener("click", (event) => {
  balanceVisible = !balanceVisible;
  updateBalanceDisplay();
  event.currentTarget.setAttribute("aria-pressed", String(!balanceVisible));
  event.currentTarget.setAttribute("aria-label", balanceVisible ? "Hide balance" : "Show balance");
});

document.querySelectorAll(".filter-button").forEach((button) => {
  button.addEventListener("click", () => {
    activeTransactionFilter = button.dataset.filter;
    document.querySelectorAll(".filter-button").forEach((item) => item.classList.toggle("selected", item === button));
    applyTransactionFilter();
  });
});

document.querySelector("#exchange-amount").addEventListener("input", () => {
  window.clearTimeout(exchangeTimeout);
  exchangeTimeout = window.setTimeout(updateExchange, 350);
});
["#from-currency", "#to-currency"].forEach((selector) => {
  document.querySelector(selector).addEventListener("change", updateExchange);
});

document.querySelector("[data-scroll-accounts]").addEventListener("click", () => {
  document.querySelector("#accounts").scrollIntoView({ behavior: "smooth", block: "center" });
});

document.querySelector("#mobile-menu").addEventListener("click", (event) => {
  const open = document.querySelector("#sidebar").classList.toggle("open");
  event.currentTarget.setAttribute("aria-expanded", String(open));
});

document.querySelectorAll(".side-nav .nav-item").forEach((item) => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".side-nav .nav-item").forEach((link) => link.classList.toggle("active", link === item));
    document.querySelector("#sidebar").classList.remove("open");
    document.querySelector("#mobile-menu").setAttribute("aria-expanded", "false");
  });
});

document.querySelector("#all-accounts").addEventListener("click", () => showToast("You’re viewing all your currency accounts."));
document.querySelector("#all-transactions").addEventListener("click", () => showToast("Showing your latest transactions."));
const bankLinkDialog = document.querySelector("#bank-link-dialog");
const bankLinkForm = document.querySelector("#bank-link-form");
document.querySelector("#add-account").addEventListener("click", () => {
  document.querySelector("#bank-link-status").textContent = "";
  document.querySelector("#bank-link-status").classList.remove("error");
  bankLinkDialog.showModal();
});
document.querySelector("#close-bank-link-dialog").addEventListener("click", () => bankLinkDialog.close());
bankLinkDialog.addEventListener("click", (event) => {
  if (event.target === bankLinkDialog) bankLinkDialog.close();
});
bankLinkForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(bankLinkForm);
  const status = document.querySelector("#bank-link-status");
  const button = document.querySelector("#bank-link-submit");
  button.disabled = true;
  status.textContent = "Requesting a secure link from Mono…";
  status.classList.remove("error");
  try {
    const response = await apiFetch("/api/bank-links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: form.get("name"), email: form.get("email") }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Mono could not start bank linking.");
    const monoUrl = new URL(result.mono_url);
    if (monoUrl.protocol !== "https:" || monoUrl.hostname !== "link.mono.co") {
      throw new Error("Mono returned an unexpected bank-link address.");
    }
    window.location.assign(monoUrl.href);
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : "Could not start secure bank linking.";
    status.classList.add("error");
    button.disabled = false;
  }
});
document.querySelector("#help-link").addEventListener("click", () => {
  if (!crispWebsiteId) {
    showToast("Crisp support is not configured yet.");
    return;
  }
  window.$crisp = window.$crisp || [];
  window.CRISP_WEBSITE_ID = crispWebsiteId;
  if (!document.querySelector('script[data-crisp-loader]')) {
    const script = document.createElement("script");
    script.src = "https://client.crisp.chat/l.js";
    script.async = true;
    script.dataset.crispLoader = "true";
    document.head.append(script);
  }
  window.$crisp.push(["do", "chat:show"]);
  window.$crisp.push(["do", "chat:open"]);
});
let smileSdkPromise;
function loadSmileSdk() {
  if (window.SmileIdentity) return Promise.resolve();
  if (!smileSdkPromise) {
    smileSdkPromise = new Promise((resolve, reject) => {
      const existingScript = document.querySelector('script[data-smile-sdk]');
      if (existingScript) {
        existingScript.addEventListener("load", resolve, { once: true });
        existingScript.addEventListener("error", reject, { once: true });
        return;
      }
      const script = document.createElement("script");
      script.src = "https://cdn.usesmileid.com/inline/v12/js/script.min.js";
      script.async = true;
      script.dataset.smileSdk = "true";
      script.addEventListener("load", resolve, { once: true });
      script.addEventListener("error", () => reject(new Error("Could not load the Smile ID verification flow.")), { once: true });
      document.head.append(script);
    }).catch((error) => {
      smileSdkPromise = undefined;
      throw error;
    });
  }
  return smileSdkPromise;
}

async function loadKycStatus() {
  const statusElement = document.querySelector("#kyc-status");
  try {
    const response = await apiFetch("/api/kyc/status");
    const result = await response.json();
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error(result.error || "Could not load identity status.");
    const labels = {
      not_started: "Identity verification has not started.",
      pending: "Verification submitted. Waiting for Smile ID to confirm the result.",
      verified: "Identity verification complete.",
      rejected: "Verification needs attention. Please contact support.",
      review: "Verification is under review. We’ll update this status when it is complete.",
    };
    statusElement.textContent = labels[result.status] || "Identity verification status unavailable.";
  } catch (error) {
    statusElement.textContent = error instanceof Error ? error.message : "Could not load identity status.";
  }
}

document.querySelector("#start-kyc").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const statusElement = document.querySelector("#kyc-status");
  if (!document.querySelector("#kyc-consent").checked) {
    statusElement.textContent = "Please review the privacy notice and consent before continuing.";
    return;
  }
  if (!smileReady) {
    statusElement.textContent = "Identity verification is unavailable until Smile ID and the public privacy notice are configured.";
    return;
  }
  button.disabled = true;
  statusElement.textContent = "Preparing secure Smile ID verification…";
  try {
    const response = await apiFetch("/api/kyc/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ consent: true }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not start identity verification.");
    await loadSmileSdk();
    if (typeof window.SmileIdentity !== "function") {
      throw new Error("Smile ID verification did not load. Please try again.");
    }
    window.SmileIdentity({
      token: result.token,
      product: "biometric_kyc",
      callback_url: result.callback_url,
      environment: result.environment,
      partner_details: result.partner_details,
      onResult: (verification) => {
        if (verification.status === "success") {
          statusElement.textContent = "Verification submitted. Smile ID will confirm the result securely.";
          loadKycStatus();
        } else if (verification.status === "failure") {
          statusElement.textContent = "Smile ID could not submit verification. Review your details and try again.";
        } else if (verification.status === "cancelled") {
          statusElement.textContent = "Verification cancelled. You can continue later.";
        }
        button.disabled = false;
      },
    });
  } catch (error) {
    statusElement.textContent = error instanceof Error ? error.message : "Could not start identity verification.";
    button.disabled = false;
  }
});
document.querySelector(".notification-button").addEventListener("click", () => showToast("You’re all caught up."));
document.querySelector(".profile-button").addEventListener("click", () => showToast("Account settings are coming soon."));
document.querySelector(".top-avatar").addEventListener("click", () => showToast("Account settings are coming soon."));
document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    const response = await apiFetch("/api/auth/logout", { method: "POST" });
    if (!response.ok) throw new Error("Could not sign out. Please refresh and try again.");
    window.location.replace("/login");
  } catch (error) {
    showToast(error instanceof Error ? error.message : "Could not sign out.");
  }
});

updateExchange();
loadDashboard();
loadIntegrationStatus();
loadKycStatus();
apiFetch("/api/auth/me").then(async (response) => {
  if (response.status === 401) {
    window.location.replace("/login");
    return;
  }
  const result = await response.json();
  if (response.ok && result.user?.name) {
    document.querySelector(".profile-copy strong").textContent = result.user.name;
    document.querySelector("#cardholder-name").textContent = result.user.name;
    document.querySelector(".welcome-row h1").firstChild.textContent =
      `${greeting}, ${result.user.name.split(/\s+/)[0]} `;
    if (result.user.role === "admin") {
      document.querySelector("#admin-nav").hidden = false;
    } else {
      const setupResponse = await apiFetch("/api/admin/setup-status");
      if (!setupResponse.ok) throw new Error("Could not verify administrator setup status.");
      const setup = await setupResponse.json();
      document.querySelector("#admin-setup-nav").hidden = !setup.available;
    }
  }
}).catch((error) => {
  setApiStatus(error instanceof Error ? error.message : "Could not verify your session.", true);
});
const paymentResult = new URLSearchParams(window.location.search).get("payment");
if (paymentResult === "success") showToast("Payment verified by Paystack.");
if (paymentResult === "failed") showToast("Payment was not completed.");
if (new URLSearchParams(window.location.search).get("bank_link") === "returned") {
  showToast("Bank connection returned. Waiting for Mono to confirm the account.");
  loadDashboard();
}
