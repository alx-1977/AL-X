// Whether Friedl is here or away, as AL/X recorded it when he told her
// (2026-10-15). A quiet indicator, not a control: she sets it.
const POLL_MS = 5000;
const SVG_NS = "http://www.w3.org/2000/svg";

const root = document.getElementById("presence");

function glyph() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  const head = document.createElementNS(SVG_NS, "circle");
  head.setAttribute("cx", "8");
  head.setAttribute("cy", "5.4");
  head.setAttribute("r", "2.6");
  const body = document.createElementNS(SVG_NS, "path");
  body.setAttribute("d", "M2.8 14c.5-2.9 2.6-4.6 5.2-4.6s4.7 1.7 5.2 4.6");
  svg.append(head, body);
  return svg;
}

const label = document.createElement("span");
const dot = document.createElement("i");
root.append(glyph(), dot, label);

function clock(iso) {
  const at = new Date(iso);
  return Number.isNaN(at.getTime())
    ? ""
    : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function show(presence) {
  if (!presence) {
    root.hidden = true;
    return;
  }
  const away = presence.state === "away";
  const since = presence.since ? clock(presence.since) : "";
  root.hidden = false;
  root.dataset.state = away ? "away" : "here";
  label.textContent = away ? "Away" : "";
  root.title = `${away ? "Away" : "Here"}${since ? ` since ${since}` : ""}`;
  root.setAttribute("aria-label", root.title);
}

// In the menu bar when there is one (it is drawn on days with BHL events),
// as a menu bar item just left of the BHL item; otherwise on its own at the
// top left. Moved, never copied: there is one indicator.
const corner = root.parentNode;
function place() {
  const bar = document.querySelector(".rt-bar");
  if (bar) {
    if (root.parentNode !== bar) bar.insertBefore(root, bar.querySelector(".rt-bar__item"));
    root.classList.add("presence--bar");
  } else if (root.parentNode !== corner) {
    corner.append(root);
    root.classList.remove("presence--bar");
  }
}
place();
new MutationObserver(place).observe(document.body, { childList: true, subtree: true });

async function poll() {
  try {
    const response = await fetch("/presence.json", { cache: "no-store" });
    if (response.ok) show((await response.json()).presence);
  } catch (error) {
    // AL/X is restarting: keep what is shown and try again.
  }
}

poll();
setInterval(poll, POLL_MS);
