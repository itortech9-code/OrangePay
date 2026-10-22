const loginForm = document.querySelector("#login-form");
const registerForm = document.querySelector("#register-form");
const verifyForm = document.querySelector("#verify-form");
const forgotForm = document.querySelector("#forgot-form");
const resetForm = document.querySelector("#reset-form");
const authStatus = document.querySelector("#auth-status");
const authTitle = document.querySelector("#auth-title");
const authIntro = document.querySelector("#auth-intro");
const registerButton = registerForm.querySelector('[type="submit"]');
const legalConfigWarning = document.querySelector("#legal-config-warning");
let activeChallengeId = "";
let activeChallengePurpose = "";
let activeResetToken = "";

function setAuthStatus(message, isError = false) {
  authStatus.textContent = message;
  authStatus.classList.toggle("error", isError);
}

function selectAuthMode(mode) {
  const loginSelected = mode === "login";
  loginForm.hidden = !loginSelected;
  registerForm.hidden = loginSelected;
  verifyForm.hidden = true;
  forgotForm.hidden = true;
  resetForm.hidden = true;
  document.querySelector("#login-tab").classList.toggle("selected", loginSelected);
  document.querySelector("#register-tab").classList.toggle("selected", !loginSelected);
  document.querySelector("#login-tab").setAttribute("aria-selected", String(loginSelected));
  document.querySelector("#register-tab").setAttribute("aria-selected", String(!loginSelected));
  authTitle.textContent = loginSelected ? "Welcome back" : "Create your account";
  authIntro.textContent = loginSelected
    ? "Sign in with your email or phone and password."
    : "Create a secure account with verified contact details.";
  setAuthStatus("");
}

function setSubmitting(form, submitting) {
  form.querySelector('[type="submit"]').disabled = submitting;
}

async function readResponse(response) {
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("The server returned an invalid response. Please try again.");
  }
  if (!response.ok) throw new Error(result.error || "Could not complete your request.");
  return result;
}

function beginCodeVerification(result, purpose) {
  activeChallengeId = result.challenge_id;
  activeChallengePurpose = purpose;
  loginForm.hidden = true;
  registerForm.hidden = true;
  verifyForm.hidden = false;
  document.querySelector("#verify-copy").textContent = result.message;
  verifyForm.elements.code.value = "";
  verifyForm.elements.code.focus();
  setAuthStatus("");
}

document.querySelector("#login-tab").addEventListener("click", () => selectAuthMode("login"));
document.querySelector("#register-tab").addEventListener("click", () => selectAuthMode("register"));
document.querySelector("#forgot-password").addEventListener("click", () => {
  loginForm.hidden = true;
  registerForm.hidden = true;
  verifyForm.hidden = true;
  forgotForm.hidden = false;
  resetForm.hidden = true;
  authTitle.textContent = "Reset your password";
  authIntro.textContent = "Recover access using your verified email or phone.";
  forgotForm.elements.identifier.value = loginForm.elements.identifier.value;
  setAuthStatus("");
});

document.querySelectorAll('input[name="contact-type"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const useEmail = radio.value === "email" && radio.checked;
    const emailInput = registerForm.elements.email;
    const phoneInput = registerForm.elements.phone;
    document.querySelector("#email-contact-label").hidden = !useEmail;
    document.querySelector("#phone-contact-label").hidden = useEmail;
    emailInput.disabled = !useEmail;
    emailInput.required = useEmail;
    phoneInput.disabled = useEmail;
    phoneInput.required = !useEmail;
  });
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSubmitting(loginForm, true);
  setAuthStatus("Checking your sign-in details…");
  try {
    const form = new FormData(loginForm);
    const result = await readResponse(await apiFetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        identifier: form.get("identifier"),
        password: form.get("password"),
      }),
    }));
    beginCodeVerification(result, "login");
  } catch (error) {
    setAuthStatus(error instanceof Error ? error.message : "Could not sign in.", true);
  } finally {
    setSubmitting(loginForm, false);
  }
});

registerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSubmitting(registerForm, true);
  setAuthStatus("Creating your account…");
  try {
    const form = new FormData(registerForm);
    const contactType = form.get("contact-type");
    const payload = {
      name: form.get("name"),
      password: form.get("password"),
      accepted_terms: form.get("accepted_terms") === "on",
    };
    payload[contactType] = form.get(contactType);
    const result = await readResponse(await apiFetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    beginCodeVerification(result, "register");
  } catch (error) {
    setAuthStatus(error instanceof Error ? error.message : "Could not create your account.", true);
  } finally {
    setSubmitting(registerForm, false);
  }
});

verifyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSubmitting(verifyForm, true);
  setAuthStatus("Verifying your code…");
  try {
    const result = await readResponse(await apiFetch("/api/auth/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        challenge_id: activeChallengeId,
        code: verifyForm.elements.code.value,
      }),
    }));
    if (result.status !== "authenticated") throw new Error("Could not start your secure session.");
    if (activeChallengePurpose === "password_reset") {
      activeResetToken = result.reset_token;
      verifyForm.hidden = true;
      resetForm.hidden = false;
      authTitle.textContent = "Choose a new password";
      authIntro.textContent = "Set a new password to secure your account.";
      setAuthStatus("");
      return;
    }
    window.location.replace("/");
  } catch (error) {
    setAuthStatus(error instanceof Error ? error.message : "Could not verify your code.", true);
  } finally {
    setSubmitting(verifyForm, false);
  }
});

forgotForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSubmitting(forgotForm, true);
  setAuthStatus("Requesting a recovery code…");
  try {
    const result = await readResponse(await apiFetch("/api/auth/forgot-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier: forgotForm.elements.identifier.value }),
    }));
    if (result.challenge_id) {
      beginCodeVerification(result, "password_reset");
    } else {
      setAuthStatus(result.message);
    }
  } catch (error) {
    setAuthStatus(error instanceof Error ? error.message : "Could not request a recovery code.", true);
  } finally {
    setSubmitting(forgotForm, false);
  }
});

resetForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSubmitting(resetForm, true);
  setAuthStatus("Updating your password…");
  try {
    const result = await readResponse(await apiFetch("/api/auth/reset-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reset_token: activeResetToken,
        password: resetForm.elements.password.value,
      }),
    }));
    if (result.status !== "password_updated") throw new Error("Could not update your password.");
    activeResetToken = "";
    resetForm.hidden = true;
    loginForm.hidden = false;
    authTitle.textContent = "Password updated";
    authIntro.textContent = "Sign in with your new password.";
    setAuthStatus("Password updated. Other active sessions have been signed out.");
  } catch (error) {
    setAuthStatus(error instanceof Error ? error.message : "Could not update your password.", true);
  } finally {
    setSubmitting(resetForm, false);
  }
});

document.querySelector("#resend-code").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  setAuthStatus("Requesting a new code…");
  try {
    const result = await readResponse(await apiFetch("/api/auth/resend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ challenge_id: activeChallengeId }),
    }));
    setAuthStatus(result.message);
  } catch (error) {
    setAuthStatus(error instanceof Error ? error.message : "Could not resend your code.", true);
  } finally {
    button.disabled = false;
  }
});

apiFetch("/api/auth/me").then((response) => {
  if (response.ok) {
    window.location.replace("/");
  } else if (response.status !== 401) {
    setAuthStatus("Could not verify your current session. Please reload and try again.", true);
  }
}).catch(() => {
  setAuthStatus("Could not reach Soltor to verify your session. Check your connection and reload.", true);
});

apiFetch("/api/public/config").then(async (response) => {
  const config = await response.json();
  if (!response.ok) throw new Error(config.error || "Could not load account terms.");
  if (
    typeof config.terms_url !== "string" ||
    typeof config.privacy_url !== "string" ||
    !config.terms_url ||
    !config.privacy_url
  ) {
    throw new Error("Configure public HTTPS links for the Terms and Privacy Notice.");
  }
  let termsUrl;
  let privacyUrl;
  try {
    termsUrl = new URL(config.terms_url);
    privacyUrl = new URL(config.privacy_url);
  } catch {
    throw new Error("Configure valid HTTPS links for the Terms and Privacy Notice.");
  }
  if (termsUrl.protocol !== "https:" || privacyUrl.protocol !== "https:") {
    throw new Error("Terms and Privacy Notice must use publicly available HTTPS links.");
  }
  const termsLink = document.querySelector("#terms-link");
  const privacyLink = document.querySelector("#privacy-link");
  termsLink.href = termsUrl.href;
  privacyLink.href = privacyUrl.href;
  termsLink.removeAttribute("aria-disabled");
  privacyLink.removeAttribute("aria-disabled");
  legalConfigWarning.hidden = true;
  registerButton.disabled = false;
}).catch((error) => {
  legalConfigWarning.textContent = error instanceof Error
    ? `Account creation is unavailable: ${error.message}`
    : "Account creation is unavailable until Terms and Privacy Notice links are configured.";
});
