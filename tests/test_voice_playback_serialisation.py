"""One voice: at most one utterance is ever audible.

The server already serialises generation — speech, typed input, mail and other
background events, and autonomous turns all funnel through one queue and one
synthesis path. What it cannot do is keep that ordering after the audio leaves
it: `audio.end` says a stream finished *arriving*, not that it finished
*playing*, so an event whose turn ran while the previous blob was still audible
used to start a second Audio element over the top of it.

These tests run the real playback functions out of `app.js` under Node against
fake Audio and Blob objects, so the queueing behaviour is genuinely exercised
rather than pattern-matched. Where a check can only be structural it says so.

A caveat worth stating plainly: this is not a browser. Node's fakes cannot
prove that two real `<audio>` elements do not overlap in a real output device,
nor reproduce autoplay policy, codec faults or device preemption. What is
proved here is the control flow that decides how many elements can exist and
when the next one starts. Real behavioural confidence still needs a browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


APP = Path(__file__).resolve().parents[1] / "src/alx/interfaces/assets/app.js"
NODE = shutil.which("node")


# Everything `app.js` touches at module scope, stubbed just enough to let the
# playback functions run. The functions under test are the file's own.
HARNESS = r"""
const listeners = {};
function element() {
  return {
    dataset: {}, textContent: "", className: "",
    append() {}, replaceChildren() {}, remove() {},
    addEventListener(name, fn) { listeners[name] = fn; },
    querySelector() { return element(); },
    get childElementCount() { return 0; },
    get firstElementChild() { return null; },
    scrollTop: 0, scrollHeight: 0,
  };
}
globalThis.document = { querySelector: () => element(), body: { dataset: {} },
                        createElement: () => element() };
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.performance = { now: () => 0 };
globalThis.setInterval = () => 0;
globalThis.WebSocket = { OPEN: 1 };
Object.defineProperty(globalThis, "navigator", {
  value: { mediaDevices: {} }, configurable: true, writable: true });
globalThis.AudioContext = function () {};
globalThis.URL = { createObjectURL: () => "blob:fake", revokeObjectURL() {} };

// Records every blob built, so merged utterances are visible.
globalThis.__blobs = [];
globalThis.Blob = function (parts, options) {
  this.parts = parts.slice();
  this.type = options && options.type;
  globalThis.__blobs.push(this);
};

// Every Audio element ever constructed, and how many are playing at once.
globalThis.__audios = [];
globalThis.__concurrent = 0;
globalThis.__peakConcurrent = 0;
// Two independent decisions: whether the element loads, and whether play()
// is allowed. Sharing one hook made a "refuse once" stub consume its turn
// during load and never reach play at all.
globalThis.__loadBehaviour = () => "ok";
globalThis.__playBehaviour = () => "ok";
globalThis.Audio = function () {
  const self = this;
  this.playing = false;
  this.handlers = {};
  this.addEventListener = (name, fn) => { self.handlers[name] = fn; };
  this.load = () => {
    // Deliver `canplay` (or a load error) on a later turn, as a browser would.
    queueMicrotask(() => {
      const mode = globalThis.__loadBehaviour(self);
      if (mode === "loaderror") {
        if (self.handlers.error) self.handlers.error();
        else if (self.onerror) self.onerror();
        return;
      }
      if (self.handlers.canplay) self.handlers.canplay();
    });
  };
  this.play = () => {
    const mode = globalThis.__playBehaviour(self);
    if (mode === "refuse") return Promise.reject(new Error("autoplay blocked"));
    self.playing = true;
    globalThis.__concurrent += 1;
    globalThis.__peakConcurrent = Math.max(
      globalThis.__peakConcurrent, globalThis.__concurrent);
    return Promise.resolve();
  };
  // Tests end playback explicitly so ordering is deterministic.
  this.finish = (how) => {
    if (self.playing) { self.playing = false; globalThis.__concurrent -= 1; }
    if (how === "error") self.onerror && self.onerror();
    else self.onended && self.onended();
  };
  globalThis.__audios.push(this);
};

async function settle(times = 30) {
  for (let i = 0; i < times; i += 1) await Promise.resolve();
}
globalThis.settle = settle;
"""


def run_js(body: str) -> dict:
    """Execute the real app.js functions with a fake browser, return results."""
    source = APP.read_text()
    # `app.js` ends by wiring DOM handlers; only the functions are needed.
    script = f"{HARNESS}\n{source}\n(async () => {{\n{body}\n}})();"
    finished = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    if finished.returncode != 0:
        raise AssertionError(
            f"node failed: {finished.stderr[-2000:]}"
        )
    for line in reversed(finished.stdout.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no result from node; stdout={finished.stdout[-2000:]}")


@unittest.skipIf(NODE is None, "node is required to execute the client logic")
class PlaybackSerialisationTests(unittest.TestCase):
    """The invariant: one audible utterance at a time, whatever arrives."""

    def test_a_second_utterance_does_not_play_over_the_first(self) -> None:
        """The reported bug: mail speech starting over a Core response."""
        result = run_js(textwrap.dedent("""
            audioParts = ["core-response"];
            enqueueUtterance("audio/mpeg");
            await settle();
            // An email event arrives while the first is still audible.
            audioParts = ["mail-event"];
            enqueueUtterance("audio/mpeg");
            await settle();
            console.log(JSON.stringify({
              audios: globalThis.__audios.length,
              peak: globalThis.__peakConcurrent,
              queued: playbackQueue.length,
              active: playbackActive,
            }));
        """))
        self.assertEqual(result["peak"], 1, "two utterances were audible at once")
        self.assertEqual(result["audios"], 1, "a second Audio element was created")
        self.assertEqual(result["queued"], 1, "the second utterance was not queued")
        self.assertTrue(result["active"])

    def test_concurrent_external_tasks_keep_independent_rows(self) -> None:
        result = run_js(textwrap.dedent("""
            showTask({task_id: "review-1", state: "waiting_for_result",
                      service: "qodo", subject: "PR #21", elapsed_seconds: 10});
            showTask({task_id: "review-2", state: "requested",
                      service: "qodo", subject: "PR #22", elapsed_seconds: 2});
            const labels = [...runningTasks.values()].map(task => task.label.textContent);
            console.log(JSON.stringify({size: runningTasks.size, labels}));
        """))
        self.assertEqual(result["size"], 2)
        self.assertIn("Waiting for result · qodo · PR #21", result["labels"])
        self.assertIn("Requested · qodo · PR #22", result["labels"])

    def test_completed_task_stops_its_clock_without_replacing_another(self) -> None:
        result = run_js(textwrap.dedent("""
            showTask({task_id: "review-1", state: "waiting_for_result",
                      service: "qodo", subject: "PR #21", elapsed_seconds: 10});
            showTask({task_id: "review-2", state: "waiting_for_result",
                      service: "qodo", subject: "PR #22", elapsed_seconds: 5});
            showTask({task_id: "review-1", state: "completed",
                      service: "qodo", subject: "PR #21 @ aaaaaaa", elapsed_seconds: 12});
            const first = runningTasks.get("review-1");
            const second = runningTasks.get("review-2");
            console.log(JSON.stringify({
              size: runningTasks.size,
              firstSettled: first.settled,
              firstLabel: first.label.textContent,
              secondSettled: second.settled,
            }));
        """))
        self.assertEqual(result["size"], 2)
        self.assertTrue(result["firstSettled"])
        self.assertIn("Completed", result["firstLabel"])
        self.assertFalse(result["secondSettled"])

    def test_terminal_task_is_removed_after_bounded_retention(self) -> None:
        result = run_js(textwrap.dedent("""
            let now = 0;
            performance.now = () => now;
            showTask({task_id: "review-1", state: "completed",
                      service: "qodo", subject: "PR #21", elapsed_seconds: 12});
            const row = runningTasks.get("review-1").row;
            let removals = 0;
            row.remove = () => { removals += 1; };
            now = terminalTaskRetentionMilliseconds + 1;
            paintTasks();
            console.log(JSON.stringify({size: runningTasks.size, removals}));
        """))
        self.assertEqual(result["size"], 0)
        self.assertEqual(result["removals"], 1)

    def test_an_unavailable_observer_is_a_bounded_terminal_status(self) -> None:
        result = run_js(textwrap.dedent("""
            showTask({task_id: "review-1", state: "observer_unavailable",
                      service: "qodo", subject: "PR #21", elapsed_seconds: 12});
            const task = runningTasks.get("review-1");
            console.log(JSON.stringify({
              settled: task.settled,
              label: task.label.textContent,
              expires: task.expiresAt,
            }));
        """))
        self.assertTrue(result["settled"])
        self.assertIn("Observer unavailable", result["label"])
        self.assertEqual(result["expires"], 10_000)

    def test_blocked_browser_storage_does_not_break_session_control(self) -> None:
        result = run_js(textwrap.dedent("""
            localStorage.getItem = () => { throw new Error("blocked"); };
            localStorage.setItem = () => { throw new Error("blocked"); };
            const restored = conversationId();
            handleControl({type: "session.ready", conversation_id: "c1", sample_rate_hz: 16000});
            await settle();
            console.log(JSON.stringify({restored, phase: document.body.dataset.phase ?? ""}));
        """))
        self.assertEqual(result["restored"], "")

    def test_task_state_is_never_written_to_browser_storage(self) -> None:
        result = run_js(textwrap.dedent("""
            let writes = 0;
            localStorage.setItem = () => { writes += 1; };
            showTask({task_id: "review-1", state: "waiting_for_result",
                      service: "qodo", subject: "PR #21", elapsed_seconds: 10});
            showTask({task_id: "review-1", state: "completed",
                      service: "qodo", subject: "PR #21 @ aaaaaaa", elapsed_seconds: 12});
            console.log(JSON.stringify({writes, tasks: runningTasks.size}));
        """))
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["writes"], 0)

    def test_task_renderer_has_no_persistent_browser_state_api(self) -> None:
        source = APP.read_text()
        start = source.index("function showTask(message)")
        end = source.index("\ndiagnosticClear.addEventListener", start)
        task_renderer = source[start:end]
        for persistent_api in (
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "document.cookie",
        ):
            with self.subTest(api=persistent_api):
                self.assertNotIn(persistent_api, task_renderer)

    def test_the_queued_utterance_is_not_lost(self) -> None:
        """Deferred, never dropped: it plays once the first finishes."""
        result = run_js(textwrap.dedent("""
            audioParts = ["first"];
            enqueueUtterance("audio/mpeg");
            await settle();
            audioParts = ["second"];
            enqueueUtterance("audio/mpeg");
            await settle();
            globalThis.__audios[0].finish("end");
            await settle();
            console.log(JSON.stringify({
              audios: globalThis.__audios.length,
              peak: globalThis.__peakConcurrent,
              queued: playbackQueue.length,
              blobs: globalThis.__blobs.map((b) => b.parts),
            }));
        """))
        self.assertEqual(result["audios"], 2, "the queued utterance never played")
        self.assertEqual(result["peak"], 1, "the two overlapped")
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["blobs"], [["first"], ["second"]])

    def test_chunks_of_separate_utterances_never_merge(self) -> None:
        """Each blob is sealed at `audio.end`, before the next can arrive."""
        result = run_js(textwrap.dedent("""
            audioParts = ["a1", "a2"];
            enqueueUtterance("audio/mpeg");
            audioParts.push("b1");
            audioParts.push("b2");
            enqueueUtterance("audio/mpeg");
            await settle();
            console.log(JSON.stringify({
              blobs: globalThis.__blobs.map((b) => b.parts),
              leftover: audioParts,
            }));
        """))
        self.assertEqual(result["blobs"], [["a1", "a2"], ["b1", "b2"]])
        self.assertEqual(result["leftover"], [])

    def test_ordering_follows_the_server(self) -> None:
        """Playback order is the order the Core decided, not a new one."""
        result = run_js(textwrap.dedent("""
            for (const name of ["one", "two", "three"]) {
              audioParts = [name];
              enqueueUtterance("audio/mpeg");
              await settle(3);
            }
            for (let i = 0; i < 3; i += 1) {
              await settle();
              const audio = globalThis.__audios[globalThis.__audios.length - 1];
              audio.finish("end");
            }
            await settle();
            console.log(JSON.stringify({
              blobs: globalThis.__blobs.map((b) => b.parts[0]),
              peak: globalThis.__peakConcurrent,
              audios: globalThis.__audios.length,
            }));
        """))
        self.assertEqual(result["blobs"], ["one", "two", "three"])
        self.assertEqual(result["peak"], 1)
        self.assertEqual(result["audios"], 3)

    def test_speech_still_works_when_nothing_is_playing(self) -> None:
        """The ordinary case: idle, one utterance, plays immediately."""
        result = run_js(textwrap.dedent("""
            audioParts = ["only"];
            enqueueUtterance("audio/mpeg");
            await settle();
            const audio = globalThis.__audios[0];
            const wasPlaying = audio.playing;
            audio.finish("end");
            await settle();
            console.log(JSON.stringify({
              played: wasPlaying,
              active: playbackActive,
              sending: sending,
              phase: document.body.dataset.phase,
            }));
        """))
        self.assertTrue(result["played"], "an idle utterance did not play")
        self.assertFalse(result["active"])
        self.assertTrue(result["sending"], "the microphone was not resumed")
        self.assertEqual(result["phase"], "listening")

    def test_an_async_event_speaks_normally_when_alx_is_idle(self) -> None:
        """A mail event with nothing ahead of it is not delayed at all."""
        result = run_js(textwrap.dedent("""
            audioParts = ["mail-event"];
            enqueueUtterance("audio/mpeg");
            await settle();
            console.log(JSON.stringify({
              audios: globalThis.__audios.length,
              playing: globalThis.__audios[0].playing,
              queued: playbackQueue.length,
            }));
        """))
        self.assertEqual(result["audios"], 1)
        self.assertTrue(result["playing"])
        self.assertEqual(result["queued"], 0)


@unittest.skipIf(NODE is None, "node is required to execute the client logic")
class PlaybackFailureTests(unittest.TestCase):
    """A failed utterance must not wedge the voice permanently."""

    def test_a_playback_error_releases_the_queue(self) -> None:
        result = run_js(textwrap.dedent("""
            audioParts = ["breaks"];
            enqueueUtterance("audio/mpeg");
            await settle();
            audioParts = ["follows"];
            enqueueUtterance("audio/mpeg");
            await settle();
            globalThis.__audios[0].finish("error");
            await settle();
            const second = globalThis.__audios[1];
            const midway = { playing: second.playing, active: playbackActive };
            second.finish("end");
            await settle();
            console.log(JSON.stringify({
              audios: globalThis.__audios.length,
              queued: playbackQueue.length,
              midwayPlaying: midway.playing,
              midwayActive: midway.active,
              finalActive: playbackActive,
              sending: sending,
            }));
        """))
        self.assertEqual(result["audios"], 2, "the queue stalled after an error")
        self.assertEqual(result["queued"], 0)
        # The failed blob is dropped and the next one takes the voice, so the
        # drainer is still legitimately holding it here.
        self.assertTrue(result["midwayPlaying"], "the next utterance never began")
        self.assertTrue(result["midwayActive"])
        # Once nothing is left, the voice is released and the microphone opens.
        self.assertFalse(result["finalActive"], "playback stayed stuck")
        self.assertTrue(result["sending"])

    def test_a_refused_autoplay_does_not_stick(self) -> None:
        """A rejected play() must settle the utterance, not hang the drainer."""
        result = run_js(textwrap.dedent("""
            globalThis.__playBehaviour = () => "refuse";
            audioParts = ["refused"];
            enqueueUtterance("audio/mpeg");
            await settle(60);
            console.log(JSON.stringify({
              active: playbackActive,
              sending: sending,
              queued: playbackQueue.length,
              phase: document.body.dataset.phase,
            }));
        """))
        self.assertFalse(result["active"], "playback stayed permanently active")
        self.assertTrue(result["sending"], "the microphone never resumed")
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["phase"], "listening")

    def test_a_load_failure_does_not_stick(self) -> None:
        result = run_js(textwrap.dedent("""
            globalThis.__loadBehaviour = () => "loaderror";
            audioParts = ["unloadable"];
            enqueueUtterance("audio/mpeg");
            await settle(60);
            console.log(JSON.stringify({
              active: playbackActive,
              sending: sending,
              phase: document.body.dataset.phase,
            }));
        """))
        self.assertFalse(result["active"])
        self.assertTrue(result["sending"])
        self.assertEqual(result["phase"], "listening")

    def test_a_failure_does_not_block_the_utterance_behind_it(self) -> None:
        """One bad blob is dropped; the next still speaks."""
        result = run_js(textwrap.dedent("""
            // Only the first utterance is refused; the next must still play.
            let first = true;
            globalThis.__playBehaviour = () => {
              if (first) { first = false; return "refuse"; }
              return "ok";
            };
            audioParts = ["bad"];
            enqueueUtterance("audio/mpeg");
            audioParts = ["good"];
            enqueueUtterance("audio/mpeg");
            await settle(60);
            const last = globalThis.__audios[globalThis.__audios.length - 1];
            console.log(JSON.stringify({
              audios: globalThis.__audios.length,
              lastPlaying: last.playing,
              peak: globalThis.__peakConcurrent,
              queued: playbackQueue.length,
            }));
        """))
        self.assertEqual(result["audios"], 2, "the good utterance never played")
        self.assertTrue(result["lastPlaying"], "the refused blob blocked the next")
        self.assertLessEqual(result["peak"], 1)
        self.assertEqual(result["queued"], 0, "the queue did not drain")

    def test_an_empty_utterance_is_ignored(self) -> None:
        """No chunks means nothing to play, and nothing to get stuck on."""
        result = run_js(textwrap.dedent("""
            audioParts = [];
            enqueueUtterance("audio/mpeg");
            await settle();
            console.log(JSON.stringify({
              audios: globalThis.__audios.length,
              blobs: globalThis.__blobs.length,
              active: playbackActive,
            }));
        """))
        self.assertEqual(result["audios"], 0)
        self.assertEqual(result["blobs"], 0)
        self.assertFalse(result["active"])


class PlaybackStructureTests(unittest.TestCase):
    """Structural guards. These are weaker than behaviour and are not a
    substitute for it; they exist so the shape cannot silently regress."""

    def setUp(self) -> None:
        self.source = APP.read_text()

    def test_the_unguarded_entry_point_is_gone(self) -> None:
        """`playResponse` played unconditionally on every arrival."""
        self.assertNotIn("playResponse", self.source)

    def test_playback_active_is_read_as_a_guard(self) -> None:
        """It was assigned but never read, which is why nothing serialised."""
        self.assertIn("if (playbackActive) return;", self.source)

    def test_exactly_one_place_constructs_an_audio_element(self) -> None:
        self.assertEqual(self.source.count("new Audio()"), 1)

    def test_exactly_one_place_seals_a_blob(self) -> None:
        self.assertEqual(self.source.count("new Blob(audioParts"), 1)

    def test_the_speaking_phase_no_longer_clears_the_buffer(self) -> None:
        """Clearing there discarded chunks of an utterance still arriving."""
        self.assertNotIn('if (message.value === "speaking") audioParts = [];',
                         self.source)

    def test_playback_state_is_released_in_a_finally(self) -> None:
        """However the last utterance ends, the voice is released."""
        drainer = self.source[self.source.index("async function drainPlaybackQueue"):]
        drainer = drainer[: drainer.index("\n}\n")]
        self.assertIn("finally", drainer)
        self.assertIn("playbackActive = false", drainer)

    def test_no_second_notification_voice_was_added(self) -> None:
        """One voice: no event-specific speech path exists in the client."""
        lowered = self.source.lower()
        for forbidden in ("notificationvoice", "notifyspeak", "alertaudio",
                          "mailaudio", "eventvoice"):
            self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
