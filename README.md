# Amazon Smart Devices for Home Assistant

A Home Assistant custom integration for Amazon's own smart-home hardware:

| Device | Home Assistant entity |
| --- | --- |
| Amazon Smart Plug | `switch` (on/off) |
| Amazon Smart Thermostat | `climate` (mode, temperature, fan) |

## Why

Home Assistant's built-in Alexa integrations expose Echo devices, not the smart
plugs and thermostats Amazon sells. Those only show up in the Alexa app, so
they cannot be used in HA dashboards, automations or scenes. This integration
signs in to your Amazon account, finds them, and makes them regular HA
entities.

## How it works

- You sign in with your Amazon account (password plus one-time code). Your
  password is used for the sign-in only and is not stored.
- It uses the same authenticated session as the core `alexa_devices`
  integration (the [`aioamazondevices`](https://pypi.org/project/aioamazondevices/)
  library) and talks to Alexa's Smart Home GraphQL API, the one the Alexa web
  app uses.
- There is no push from Amazon, so state is **polled** (`cloud_polling`).
  Changes made in the Alexa app can take a little while to show up in HA, and
  commands go through Amazon's cloud, so expect a second or two of delay.
- Commands update HA immediately and are corrected by the next poll.

## Install

1. Copy `custom_components/amazon_smart_devices` into your HA
   `config/custom_components/` folder (or add this repository to HACS as a
   custom repository).
2. Restart Home Assistant.
3. Settings > Devices & services > Add integration > **Amazon Smart Devices**.
4. Sign in with your Amazon account and enter the one-time code.

## Thermostat

- **Modes:** Heat, Cool, Auto and Off. In Auto there are two setpoints: a low
  (heat) and a high (cool). Auto is shown as "Auto" but is HA's Heat/Cool mode
  underneath, because that is the mode HA's UI draws two setpoints for.
- **Temperature:** target setpoint with the limits the thermostat reports
  (heating and cooling ranges differ).
- **Fan:** On, Auto and Circulate.
- **Action:** heating, cooling, idle or off, from the equipment stages.
- **Remembered setpoints:** Alexa has a single target shared by Heat and Cool.
  The integration remembers the value each mode last had and puts it back when
  you switch modes, so changing Heat to 72 does not change what Cool returns
  to. The values survive HA restarts.

### Not supported

- **Vacation mode:** Amazon's API does not expose it. Build it as an HA scene
  or automation (for example heat 80, cool 65).
- **Schedules:** use HA automations rather than Alexa's thermostat schedules.

## Development

```
uv venv --python 3.13 && uv pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Diagnostics can be downloaded from the integration's page in HA; they show the
raw state Alexa returns for each device, which helps when a device does not
behave. The `logs/` folder is git-ignored for those downloads.

## Disclaimer

This is unofficial and not affiliated with Amazon. It relies on Alexa's
private web API, which Amazon can change at any time.
