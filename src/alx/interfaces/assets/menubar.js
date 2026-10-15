// AL/X's menu bar, in the manner of macOS (Friedl, 2026-10-15): always
// there, transparent, its text white or dark to suit what is behind it.
// Like macOS, the text colour follows the brightness of the background just
// under it, sampled from the backdrop video every few seconds, with a little
// hysteresis so it does not flicker on a changing frame. Each item is judged
// by what is behind it: a backdrop dark on one side and bright on the other
// gets white text on one side and dark on the other.
const SAMPLE_MS = 2000;
const DARK_ABOVE = 0.62;  // switch to dark text when the strip is this bright
const LIGHT_BELOW = 0.52; // and back to white text below this

const bar = document.getElementById("menubar");
const clock = document.getElementById("menubar-clock");
const video = document.querySelector("video.backdrop");

function showTime() {
  clock.textContent = new Date().toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).replace(/,/g, "");
}

// The part of the video frame behind a box on the screen, given that the
// video covers the window (object-fit: cover crops it to fill).
const canvas = document.createElement("canvas");
canvas.width = 16;
canvas.height = 4;
const context = canvas.getContext("2d", { willReadFrequently: true });

function behind(rect) {
  const width = video.videoWidth, height = video.videoHeight;
  if (!width || !height || !rect.width) return null;
  const scale = Math.max(innerWidth / width, innerHeight / height);
  const left = (width * scale - innerWidth) / 2;
  const top = (height * scale - innerHeight) / 2;
  return [(rect.left + left) / scale, (top + 0) / scale,
          rect.width / scale, bar.offsetHeight / scale];
}

function brightness(region) {
  context.drawImage(video, ...region, 0, 0, canvas.width, canvas.height);
  const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
  let sum = 0;
  for (let i = 0; i < data.length; i += 4) {
    sum += (0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]) / 255;
  }
  return sum / (data.length / 4);
}

function sample() {
  if (!video) return;
  for (const item of bar.children) {
    if (item.classList.contains("rt-bar__space")) continue;
    const region = behind(item.getBoundingClientRect());
    if (!region) continue;
    try {
      const level = brightness(region);
      // The first decision splits the difference; after that, only a clear
      // change moves it.
      const middle = (DARK_ABOVE + LIGHT_BELOW) / 2;
      if (level > DARK_ABOVE || (!item.dataset.ink && level >= middle)) item.dataset.ink = "dark";
      else if (level < LIGHT_BELOW || !item.dataset.ink) item.dataset.ink = "light";
    } catch (error) {
      // A frame not ready yet: keep the colour it has.
    }
  }
}

showTime();
sample();
setInterval(() => { showTime(); sample(); }, SAMPLE_MS);
