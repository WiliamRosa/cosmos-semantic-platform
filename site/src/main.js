// Copy buttons, the click-to-load demo frame, and the latest release tag.

function flash(button, text) {
  const label = button.querySelector(".copy-label") || button;
  const previous = label.textContent;
  label.textContent = text;
  setTimeout(() => { label.textContent = previous; }, 1500);
}

async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    flash(button, "Copied");
  } catch {
    flash(button, "Press Ctrl+C");
  }
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", () => copy(button.dataset.copy, button));
});

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const source = document.getElementById(button.dataset.copyTarget);
    const commands = source.textContent
      .split("\n")
      .map((line) => line.replace(/^\$ /, ""))
      .join("\n");
    copy(commands, button);
  });
});

// The demo is a full app; load it only when the visitor asks for it.
const loader = document.getElementById("demo-load");
const frame = document.getElementById("demo-frame");
if (loader && frame) {
  loader.addEventListener("click", () => {
    frame.src = frame.dataset.src;
    loader.hidden = true;
  });
}

// Keep the hero badge on the latest release without a redeploy. The static text is the fallback.
fetch("https://api.github.com/repos/WiliamRosa/cosmos-semantic-platform/releases/latest", {
  headers: { Accept: "application/vnd.github+json" },
})
  .then((response) => (response.ok ? response.json() : null))
  .then((release) => {
    if (release && /^v\d+\.\d+\.\d+$/.test(release.tag_name)) {
      document.getElementById("release").textContent = release.tag_name;
    }
  })
  .catch(() => {});

// Contact form: posts JSON to the Worker; without JavaScript the form posts normally.
const contact = document.getElementById("contact-form");
if (contact) {
  const status = contact.querySelector(".form-status");
  const submit = contact.querySelector("button[type=submit]");
  const setStatus = (text, kind) => {
    status.textContent = text;
    status.className = `form-status ${kind || ""}`;
  };
  contact.elements.started_at.value = String(Date.now());

  const result = new URLSearchParams(location.search).get("contact");
  if (result === "sent") setStatus("Thanks, your message was sent. We will answer by email.", "ok");
  if (result === "error") setStatus("The message could not be sent. Please write to administrator@cosmosdata.ai.", "err");

  contact.addEventListener("submit", async (event) => {
    event.preventDefault();
    let firstInvalid = null;
    for (const field of contact.querySelectorAll("[required]")) {
      const valid = field.checkValidity() && field.value.trim() !== "";
      field.setAttribute("aria-invalid", String(!valid));
      if (!valid && !firstInvalid) firstInvalid = field;
    }
    if (firstInvalid) {
      setStatus("Please fill in your name, a valid email and a message.", "err");
      firstInvalid.focus();
      return;
    }

    submit.disabled = true;
    setStatus("Sending…");
    try {
      const response = await fetch(contact.action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(contact))),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) throw new Error(payload.error || "send failed");
      contact.reset();
      contact.elements.started_at.value = String(Date.now());
      setStatus("Thanks, your message was sent. We will answer by email.", "ok");
    } catch (error) {
      const known = error.message && error.message !== "send failed" && !error.message.startsWith("Failed");
      setStatus(known ? error.message : "The message could not be sent. Please write to administrator@cosmosdata.ai.", "err");
    } finally {
      submit.disabled = false;
    }
  });
}
