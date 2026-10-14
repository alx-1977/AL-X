# BHL reader schedule protocol (V2 readers)

How AL/X sends a V2 room reader its schedule, and what the reader must do with
it (D-040). The reader never fetches its own schedule: AL/X reads every
reader's schedule from BehaviorLive, checks and combines them into one
calendar, settles any client errors, and sends each reader a clean day.

## The function

The firmware exposes one Particle function:

```cpp
Particle.function("schedule", scheduleHandler);
```

AL/X calls it once per message through the Particle Cloud API. Each argument
is one compact JSON object of at most 600 bytes (UTF-8). The function returns
an integer; AL/X sends the next message only after a `0`.

## The messages

A schedule is always `begin`, then `n` `event` messages in order, then
`commit`. Every message carries the same version `v`, eight hex characters.

```json
{"op":"begin","v":"1a2b3c4d","n":2,"m":0,"r":"Majestic","o":-4}
{"op":"event","v":"1a2b3c4d","i":0,"id":3462,"st":1791381900,"en":1791385500,"a":1791345600,"u":1791383700,"t":"Ethics in practice","fn":"Pam","ln":"Beesly","hbd":0}
{"op":"event","v":"1a2b3c4d","i":1,"id":1099,"st":1791387000,"en":1791393000,"a":1791383700,"u":1791390000,"t":"Supervision","fn":"Jim","ln":"Halpert","hbd":1}
{"op":"commit","v":"1a2b3c4d"}
```

| Field | Meaning |
|---|---|
| `v` | Schedule version. The same schedule always has the same version. |
| `n` | Number of events that follow (0 to 32). `0` means no events left today. |
| `m` | Reader mode: `0` scan IN, `1` scan OUT. |
| `r` | Room name, at most 64 characters. |
| `o` | Hours to add to UTC for local display time, e.g. `-4`. |
| `i` | Position of this event, counting from 0. |
| `id` | BehaviorLive event ID. Scans are labelled with it. |
| `st`, `en` | Start and end, Unix seconds UTC. |
| `a`, `u` | The window in which the reader treats this event as current, Unix seconds UTC (D-043). |
| `t` | Title, at most 64 characters. |
| `fn`, `ln` | Presenter's first and last name, at most 32 characters each; may be empty. |
| `hbd` | Hybrid flag, `0` or `1`. |

Events arrive in start order and never overlap. Back-to-back events (one ends
exactly when the next starts) are normal.

## Return values

| Value | Meaning |
|---|---|
| `0` | Accepted. |
| `-1` | Message unreadable (not JSON, a field missing or the wrong type). |
| `-2` | No `begin` for this version. |
| `-3` | Event out of order (`i` is not the next expected position). |
| `-4` | `commit` before all `n` events arrived. |
| `-5` | More events than the reader can hold. |
| `-6` | Saving the schedule failed. |

## What the reader must do

1. **`begin`** throws away any half-received schedule and starts a new one in
   memory. The schedule the reader is running is untouched.
2. **`event`** is stored in memory if `v` matches and `i` is the next position.
3. **`commit`** with all `n` events received writes the new schedule to flash
   in one step (write a new file, then replace the old one), and only then
   switches to it. The reader runs the new schedule from that moment.
4. Anything else (a lost message, a power cut, a new `begin`) leaves the
   reader on its previous schedule. It never runs a partial one.
5. Receiving the version already running again is normal: AL/X resends a
   whole schedule when an answer was lost, and the reader simply accepts it.
6. The running schedule survives restarts and loss of signal. With no signal
   the reader keeps running it.
7. The reader tracks its current event by `id`, never by position. The
   current event is the one whose window (`a` to `u`) holds the time now; with
   none, or with the clock unset, no event is current.

## Which event is current (D-043)

AL/X decides it and sends it as each event's window, so the rule lives in one
place and can change without a firmware update. V1's rule is kept:

- **IN reader:** moves to the next event halfway through the current one, so
  attendees can scan in ahead. Its last window closes halfway through the last
  event.
- **OUT reader:** moves to the next event halfway through that next event, so
  attendees can scan out afterwards. Its last window closes 30 minutes after
  the last event ends.
- The first window opens at local midnight of the event day.

## Asking for a schedule

The reader publishes `roomreader/schedule_request` (data `{"v":"<version
held>"}`) on every new cloud connection, and every five minutes while it holds
nothing still to run. AL/X hears it on Particle's event stream and answers by
sending the schedule. AL/X also sends unasked when a reader is not holding the
schedule its calendar says it should.

## Status the reader reports

So AL/X can confirm what each reader holds, runs and shows, the firmware
publishes a status card (`roomreader/status`) whenever something on it
changes, and exposes the same JSON as the Particle variable `status`:

```json
{"v":"1a2b3c4d","e":3462,"n":2,"bat":87,"pwr":"bat","sig":60,"sq":55,"clk":1,"st":"running","scr":{"e":3462,"ok":1,"ms":1123},"fw":"5.03"}
```

| Field | Meaning |
|---|---|
| `v` | Schedule version running. |
| `e` | Event ID it is labelling scans with now, `0` for none. |
| `n` | Its event count. |
| `bat` | Battery percent (at most 100), `-1` unknown. |
| `pwr` | `usb` or `bat`. |
| `sig`, `sq` | Cellular signal strength and quality in percent, `-1` unknown. |
| `clk` | `1` when the clock is set. |
| `st` | Reader state: `starting`, `running`, `waiting` (between events), `fault-reader`, `fault-clock`, `fault-signal`, or `sleeping`. |
| `scr` | The display: `e` the event ID on it (`0` for none), `ok` `1` when its last refresh worked (judged from the panel's BUSY line), `ms` how long BUSY was held. A failed refresh (firmware 5.13) adds `why`: `none` (BUSY never asserted: no panel, or it did not respond), `stuck` (BUSY still asserted at the end), `short` (too brief for a refresh); and `boot` `1` when it was the first refresh since start-up (the panel did not initialise). `null` before the first refresh. |
| `fw` | Firmware version. |

The card waits for a display refresh in progress (up to 30 seconds), so the
refresh result goes out in the same publish.

## What the reader does in each state

- **running**: an event's window holds the time now. Only in this state does
  the card reader run; every card read is sent for that event.
- **starting**, **waiting**, **fault-…**: the card reader is off, so no card
  can be taken without an event to credit it to. A fault shows solid red.
- **fault-screen** (5.13): the last display refresh failed while the reader
  has events. No scans until a refresh works (Friedl, 2026-10-14). The
  reader resets and redraws its panel every five minutes on its own.
- **sleeping**: nothing left to run today (AL/X answered, or did not answer
  within three minutes). The reader sends its last card, sleeps, and wakes
  at 05:00 local or on a button press, then asks for its schedule again.

## Scans

Each card read is published as `send_data` (V1's payload, which
BehaviorLive's webhook takes unchanged), queued in flash until sent:

```json
{"READER_ID":"ab2d5218/scans","e":3462,"rm":0,"s":[{"Ts":"2026-10-13T10:20:25Z","Id":"4abb4ed3"}]}
```

AL/X hears the same publish on the product's event stream, at no extra data
operation, and records it in the reader log.

## Putting a fault right (firmware 5.13)

The `recover` function takes `screen` (reset the display panel and redraw it)
or `restart` (queue the open scans and restart the reader), and returns `0`
when accepted, `-1` otherwise. AL/X's reader monitor uses it for a screen
fault, once each step falls due while the fault lasts and the reader is
online: `screen` at once, `screen` again after 3 minutes, `restart` after 6;
after 12 minutes the reader needs a person (red). Each step is logged
(`screen_recovery`), and so is the fault clearing (`screen_recovered`).
