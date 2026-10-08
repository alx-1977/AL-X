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

So AL/X can confirm what each reader holds and runs, the firmware exposes a
Particle variable `status`:

```json
{"v":"1a2b3c4d","e":3462,"n":2,"bat":87,"clk":1,"fw":"5.00"}
```

`v` is the version running, `e` the event ID it is labelling scans with now
(`0` for none), `n` its event count, `bat` battery percent, `clk` `1` when
the clock is set, `fw` the firmware version. Signal and online state come from
Particle itself.
