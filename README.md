# SIA DC-09 Control Center for Home Assistant

A receiver for ANSI/SIA DC-09, the protocol alarm panels and control centre
transmitters use to report to a monitoring station. Point a panel at Home
Assistant and every account it reports becomes a device with an alarm panel,
a status sensor, a heartbeat and a two year activity log.

Everything is local. Nothing is sent anywhere, and no cloud account is needed.

## What it does

- Listens on a configurable **UDP** port, **TCP** port, or both, bound to a
  chosen address (`0.0.0.0` for every interface).
- Decrypts **AES** encrypted messages using a per-account key of 32, 48 or 64
  hexadecimal characters. The account number is in the cleartext header, which
  is what makes per-account keys possible.
- Replies **ACK**, **NAK** or **DUH**, with the NAK-on-bad-checksum behaviour
  configurable for panels that get stuck retransmitting.
- Derives an alarm **status** per account from the order of the messages
  received, including a sticky triggered and panic state.
- Keeps an **activity log** in its own SQLite database for a configurable
  retention period, 24 months by default. Automatic tests and heartbeats are
  stored but filtered out of the activity view.
- Fires **event bus** events shaped like the built-in `sia` integration's, so
  existing automations port across with little change.

## Installation

### HACS

1. In HACS, choose **Integrations**, then the overflow menu, then **Custom
   repositories**.
2. Add `https://github.com/mikkel-esp/SIADC-09HACSImplementation` with the
   category **Integration**.
3. Install **SIA DC-09 Control Center** and restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   *SIA DC-09*.

### Manually

Copy `custom_components/sia_dc09` into your Home Assistant
`config/custom_components` directory and restart.

## Configuration

Everything is configured in the UI; there is no YAML.

Initial setup asks for the receiver settings and then collects accounts one at
a time. To change anything afterwards, go to **Settings → Devices & services →
SIA DC-09 → Configure**, which offers:

- **Receiver settings** — ports, bind address, responses, retention.
- **Add an account** — another panel.
- **Edit an account** — pick an account, then change its name, key, user names
  or arming targets. Everything is prefilled with what is stored now.
- **Remove accounts**.

Saving reloads the receiver, which takes a second or two and does not lose
stored activity.

### Receiver

| Setting | Default | Notes |
| --- | --- | --- |
| Bind address | `0.0.0.0` | `0.0.0.0` listens on every interface. |
| UDP port | `10000` | Set to `0` to disable UDP. |
| TCP port | `10000` | Set to `0` to disable TCP. The same number as UDP is fine; they are separate sockets. |
| Send ACK and NAK responses | on | Most panels retry forever until acknowledged. |
| Send NAK when the checksum fails | on | Turn off for a panel that loops on a message it cannot send correctly. |
| Unknown accounts | Discover | See below. |
| Keep activity for | 24 months | Older rows are deleted twice a day. |
| Recent events on the activity sensor | 50 | How many events the last-activity sensor carries as attributes. |

**Unknown accounts** decides what happens when a message arrives for an account
you have not configured:

- **Discover** — acknowledge it and list it on the *Unknown accounts* sensor,
  together with the messages it sent and the address they came from, but create
  nothing. This is the default and is how you find out what a panel is actually
  transmitting. See *Identifying an unknown account*, below.
- **Ignore** — answer with a DUH so the panel stops retrying, and record
  nothing.
- **Create automatically** — add the account to the configuration and reload.

### Accounts

| Setting | Notes |
| --- | --- |
| Account number | 3 to 16 hexadecimal characters, as the panel transmits it. |
| Name | Used for the device and all of its entities. |
| Encryption key | Optional. 32, 48 or 64 hexadecimal characters (128, 192 or 256 bit). |
| Heartbeat timeout | Minutes of silence before the account is marked offline. Default 90. |
| Users | Optional. One `number: name` per line. See *User names*, below. |
| Ignore message timestamps | On by default. Turn it off only if the panel's clock is reliably synchronised; otherwise its messages will be rejected as replays. |
| Arm/disarm targets | Optional. See *Arming*, below. |

### User names

Panels identify whoever armed or disarmed by number, so an event reads
`Closing Report - User number 501 (area 1)`. Give the numbers names, one per
line, in the account's **Users** box — on **Add an account** during setup, or
afterwards under **Configure → Edit an account**:

```
501: Mikkel
502: Anna
503 = Guest cleaner
```

Either `:` or `=` separates the number from the name, and leading zeros are
ignored, so `501`, `0501` and `00501` are the same person — panels are not
consistent about padding. The same event then reads:

```
Closing Report - User Mikkel (area 1)
```

The name is applied wherever the event is shown: the activity sensor and its
recent-event attributes, the stored activity log, and the `user_name` field of
the bus event. The raw `user_number` is kept alongside it, so automations can
keep matching on the number.

Only fields the protocol identifies as a user number are renamed. A zone number
that happens to be 501 stays a zone, and codes where the protocol cannot say
whether a number is a zone or a user are left alone — renaming the wrong thing
is worse than renaming nothing.

## Entities

Each account becomes one device carrying:

| Entity | Notes |
| --- | --- |
| `alarm_control_panel.<name>` | The standard panel states. |
| `sensor.<name>_status` | The full status including `panic`. |
| `sensor.<name>_last_heartbeat` | When anything was last received, including automatic tests. |
| `sensor.<name>_last_activity` | The last non-test event, with recent events as attributes. |
| `binary_sensor.<name>_connectivity` | Whether the account reported inside its heartbeat window. |
| `binary_sensor.<name>_smoke` | Fire, gas, heat and sprinkler codes. |
| `binary_sensor.<name>_moisture` | Water and freeze codes. |
| `binary_sensor.<name>_power` | Mains power, from AC trouble and restore. |
| `binary_sensor.<name>_battery` | System and transmitter battery trouble. |

The receiver itself gets a *Messages received* and an *Unknown accounts* sensor.

### Status

`sensor.<name>_status` takes one of:

`disarmed`, `armed_away`, `armed_home`, `armed_night`, `armed_vacation`,
`armed_custom_bypass`, `arming`, `disarming`, `pending`, `triggered`, `panic`.

The code table is a strict superset of the one in Home Assistant's built-in
`sia` integration, so anything core recognises is recognised here too.

Status follows the order of messages received, with two deliberate rules:

- **An alarm is sticky.** Once `triggered` or `panic`, an unrelated code does
  not clear it. Only a disarm, a restore, or a stronger alarm changes it.
- **Panic outranks triggered.** A holdup during a burglary escalates. The alarm
  panel entity has no panic state of its own and reports `triggered`; the status
  sensor keeps the distinction, and the panel carries `panic: true` as an
  attribute.

Because DC-09 only reports, a disarm at the keypad whose closing report is lost
would leave Home Assistant out of date forever. Use the `sia_dc09.set_status`
service to correct it.

### Arming

DC-09 is a one way protocol: a receiver cannot command a panel. If you have some
*other* route to the panel — a cloud integration, a relay module, a keyswitch —
nominate a script, scene, button or switch as the arm-away, arm-home, arm-night
or disarm target for the account. The alarm panel entity then advertises those
features and runs your target. The status is deliberately *not* changed
optimistically: it still only follows the messages the panel actually sends, so
a target that silently fails cannot leave Home Assistant claiming the alarm is
armed.

## Automations

Two bus events are fired for every message:

- `sia_dc09_event_<ACCOUNT>` — one account, for example `sia_dc09_event_1234`.
- `sia_dc09_event` — everything.

```yaml
automation:
  - alias: Tell me about the front door
    triggers:
      - trigger: event
        event_type: sia_dc09_event_1234
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.severity == 'alarm' }}"
    actions:
      - action: notify.mobile_app
        data:
          message: "{{ trigger.event.data.summary }}"
```

### Event payload

| Key | Example | Notes |
| --- | --- | --- |
| `account` | `"1234"` | Always upper case. |
| `code` | `"BA"` | SIA code, or the Contact ID code for ADM-CID. |
| `code_title` | `"Burglary Alarm"` | |
| `summary` | `"Burglary Alarm - Zone or point 1 (area 1)"` | Ready to put in a notification. |
| `severity` | `"alarm"` | `info`, `warning` or `alarm`. |
| `category` | `"Burglary"` | |
| `message` | `null` | Free text, when the panel sent any. |
| `message_type` | `"SIA-DCS"` | Also `ADM-CID`, `NULL`, and others. |
| `receiver`, `line` | `"0"`, `"0"` | From the header. |
| `sequence` | `"0001"` | |
| `ri` | `"1"` | Receiver identifier within the message. |
| `id` | `null` | User or card identifier. |
| `user_number` | `"501"` | The user the event refers to, when there is one. |
| `user_name` | `"Mikkel"` | The name configured for that number, if any. |
| `zone` | `"001"` | The point or zone the code refers to. |
| `partition` | `null` | Area, where the panel reports one. |
| `event_qualifier` | `"N"` | SIA modifier, or the Contact ID qualifier. |
| `timestamp` | `null` | The panel's own timestamp, when present. |
| `extended_data` | `{}` | Any extended data blocks. |
| `is_test` | `false` | True for automatic tests and heartbeats. |
| `is_link_test` | `false` | True for a `NULL` link test. |
| `encrypted` | `false` | Whether the body arrived encrypted. |
| `crc_valid` | `true` | |
| `decoded` | `true` | False if the message could not be parsed. |
| `errors` | `[]` | Why it could not be parsed. |
| `status_before` | `"disarmed"` | |
| `status_after` | `"triggered"` | |
| `status_changed` | `true` | |
| `response` | `"ACK"` | `ACK`, `NAK`, `DUH` or `null`. |
| `transport` | `"udp"` | `udp` or `tcp`. |
| `port` | `10000` | The local port it arrived on. |
| `remote_ip` | `"192.0.2.10"` | |
| `received_at` | `"2025-01-01T12:00:00+00:00"` | ISO 8601, UTC. |

The shared keys (`account`, `code`, `message_type`, `zone`, `id`, `ri`, `line`,
and so on) keep the names Home Assistant's built-in `sia` integration uses.

## Services

| Service | Returns | Purpose |
| --- | --- | --- |
| `sia_dc09.get_activity` | response | Stored activity, filtered by account, time range and severity. Excludes automatic tests unless `include_tests` is set. |
| `sia_dc09.clear_activity` | — | Deletes stored activity for one account or all of them. |
| `sia_dc09.purge` | — | Applies the retention window immediately. |
| `sia_dc09.set_status` | — | Overrides an account's status when a report was lost. |
| `sia_dc09.decode_message` | response | Decodes a raw message. Accepts a hex dump, a full wire capture, or a bare message body. |

```yaml
action: sia_dc09.get_activity
data:
  account: "1234"
  limit: 20
  severity: alarm
response_variable: activity
```

## Troubleshooting

**Nothing arrives.** Check the panel is pointed at the right port and that the
bind address covers the interface it reaches you on. Use the *Messages received*
sensor to see whether anything is landing at all; if it climbs while your
account's entities stay idle, the account number does not match, and the
*Unknown accounts* sensor will tell you what the panel is really sending.

**Messages arrive but are rejected.** Download diagnostics from the integration
page: it includes the last 25 events with their errors, and redacts your keys.
A corrupt frame is logged but never allowed to change an alarm status.

**A panel retransmits forever.** It is not accepting your responses. Confirm
*Send ACK and NAK responses* is on, and try turning off *Send NAK when the
checksum fails*.

**Encrypted messages fail.** The key must be hexadecimal, 32, 48 or 64
characters, and must match the panel exactly. `sia_dc09.decode_message` will
decode a captured message against a key you supply, so you can test one without
reconfiguring anything.

**An account with a key stopped working.** Once an account has an encryption
key, unencrypted messages for it are rejected and NAKed on purpose - see
[Security](#security). Either configure the key on the panel too, or clear it
here.

### Identifying an unknown account

The *Unknown accounts* sensor counts accounts that are transmitting but are not
configured. A count on its own says nothing useful, so the sensor's attributes
carry the detail:

- `details` — one entry per account, busiest first, each with `message_count`,
  `first_seen`, `last_seen`, the originating addresses in `remote_ips`, and the
  most recent messages in `recent_messages` with their summary, code, transport
  and port.
- `dropped_messages` — messages discarded because too many distinct account
  numbers had already been seen. Anything other than zero means something is
  inventing account numbers at your receiver, not that a panel is
  misconfigured.

That is usually enough to tell a panel you forgot to add apart from traffic
that does not belong to you. The full history, including the raw frames, is in
the integration's diagnostics, and the stored messages can be read back with
`sia_dc09.get_activity` using the unknown account number — it does not have to
be configured first.

Nothing an unconfigured account sends is ever allowed to change an entity, and
under the **Ignore** policy nothing is recorded at all.

## Security

This integration listens on the network and drives alarm states, so it is worth
being explicit about what it can and cannot protect against.

### What the integration enforces

- **A key means encryption is required.** If an account has an encryption key,
  a cleartext message claiming to be from that account is rejected and never
  changes state. Without this, anyone who could reach the port could forge a
  disarm, because the account number travels in the cleartext header.
- **Replay and retransmission are not applied twice.** A byte-identical message
  for an account is acknowledged, so a panel retransmitting after a lost ACK
  behaves correctly, but it only moves the alarm state once. For an account
  that enforces timestamps, this memory lasts as long as a captured message
  would still be accepted, closing the gap between the two checks. For an
  account that ignores timestamps the window is deliberately short, because two
  identical events are then genuinely indistinguishable: there it deduplicates,
  it does not authenticate.
- **Encrypted accounts enforce timestamps by default.** Encryption proves who
  wrote a message but not *when*, so an account with a key rejects messages
  whose timestamp has drifted. This applies from the moment a key is added,
  including to an account that previously ran unencrypted. You can turn it off
  per account with *Ignore message timestamps* if the panel's clock cannot be
  trusted, but that removes the only real defence against replay.
- **Timestamp enforcement cannot be bypassed by omission.** With enforcement
  on, a message with a missing or unparseable timestamp is rejected rather
  than waved through.
- **Rejected messages never reach entities or automations.** A message that
  fails any check is written to the activity log so you can see a misbehaving
  panel, but it is not dispatched to entities or fired on the event bus.
  Otherwise a forged code could trip the smoke or power sensors.
- **Keys stay out of diagnostics and logs.** Diagnostics report only whether an
  account is encrypted.

### What the protocol cannot protect against

DC-09 mandates AES-CBC with an all-zero IV and no message authentication. The
CRC is unkeyed, so it detects corruption, not tampering. This is a property of
the wire format, not of this implementation, and cannot be fixed without
breaking compatibility with real panels.

The practical consequences:

- Encryption without authentication does not prove origin or freshness. Leave
  *Ignore message timestamps* off for encrypted accounts, which is the default,
  so a captured message stops working within about a minute.
- If you must ignore timestamps because a panel's clock is unreliable, accept
  that a captured message for that account can be replayed later. Weigh that
  against the panel going silent when its clock drifts.
- Treat network position as part of your threat model: put the receiver on a
  trusted network segment rather than exposing it to the internet.
- Duplicate detection is held in memory, so restarting Home Assistant clears
  it. A message captured moments before a restart can be replayed until its
  timestamp falls outside the accepted band.
- Use a different key for each account so one compromised panel does not
  expose the rest.

If you find a security issue, please open an issue on this repository.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Requires Python 3.12 or newer.

The protocol implementation in `custom_components/sia_dc09/dc09/` is a pure
Python port of [SIADC09Debugger](https://github.com/mikkel-esp/SIADC09Debugger),
including its test vectors, and has no Home Assistant dependency. `listener.py`
is likewise Home Assistant free and is tested over real loopback sockets.

## License

Released under the [MIT License](LICENSE).
