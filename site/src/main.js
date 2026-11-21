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
