# Home Assistant

Sonora Home gives Home Assistant a small API under `/api/ha/`, behind a bearer
token. The token reaches only `/api/ha/*` (not the rest of Sonora) and cannot
manage tokens. Starting a preset from Home Assistant takes its speakers from
any party that holds them, without asking.

## Make a token

A Home Assistant screen on the party page comes later. Until then, with a
signed-in browser session (copy the `sonora_session` cookie value):

```sh
curl -X POST https://sonora.example/api/ha/tokens \
  -H 'Content-Type: application/json' -H 'Cookie: sonora_session=...' \
  -d '{"label":"Home Assistant"}'
# {"id":"a1b2c3d4e5f6","token":"..."}   the token is shown once
```

Put it in `secrets.yaml`: `sonora_token: "<token>"`. List tokens with
`GET /api/ha/tokens`, revoke one with `DELETE /api/ha/tokens/<id>`.

## configuration.yaml

```yaml
rest_command:
  sonora_start_evening:
    url: "https://sonora.example/api/ha/presets/PRESET_ID/start"
    method: post
    headers:
      Authorization: !secret sonora_token_header   # "Bearer <token>"
  sonora_end_party:
    url: "https://sonora.example/api/ha/parties/{{ party_id }}/end"
    method: post
    headers:
      Authorization: !secret sonora_token_header

sensor:
  - platform: rest
    name: sonora_state
    resource: "https://sonora.example/api/ha/state"
    headers:
      Authorization: !secret sonora_token_header
    scan_interval: 10
    value_template: "{{ value_json.parties | length }}"
    json_attributes: [presets, parties]

template:
  - sensor:
      - name: "Sonora party"
        state: "{{ state_attr('sensor.sonora_state', 'parties')[0].name if state_attr('sensor.sonora_state', 'parties') else 'none' }}"
      - name: "Sonora track"
        state: >
          {% set p = state_attr('sensor.sonora_state', 'parties') %}
          {{ p[0].track.title if p else '' }}
        attributes:
          artist: "{{ (state_attr('sensor.sonora_state', 'parties') or [{'track': {}}])[0].track.artist }}"
          picture: "{{ (state_attr('sensor.sonora_state', 'parties') or [{'track': {}}])[0].track.art }}"
```

Put `sonora_token_header: "Bearer <token>"` in `secrets.yaml` (Home Assistant
cannot join a secret to a literal prefix). The preset ids are in
`sensor.sonora_state` attributes (`presets`).

## Dashboard button

```yaml
type: button
name: Evening music
icon: mdi:speaker-multiple
tap_action:
  action: call-service
  service: rest_command.sonora_start_evening
```

To end the running party: `service: rest_command.sonora_end_party` with
`service_data: {party_id: "<id from sensor.sonora_state>"}`.

## Endpoints

| Call | Result |
| --- | --- |
| `GET /api/ha/state` | `{presets:[{id,name}], parties:[{id,name,state,speakers,track:{title,artist,album,art}}]}` |
| `POST /api/ha/presets/{id}/start` | starts the party, taking its speakers |
| `POST /api/ha/parties/{id}/end` | ends it |

Errors are JSON `{error}`: 401 bad token, 404 `not_found`, 409 `no_speakers`,
503 `parties_unavailable`.
