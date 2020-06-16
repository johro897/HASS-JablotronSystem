# HASS-JablotronSystem
Jablotron component for Home Assistant adapted from:
- Binary Sensor, https://github.com/plaksnor/HASS-JablotronSystem, maintained by @plaksnor
- Alarm Control Panel, https://github.com/Horsi70/HASS-JablotronSystem, maintained by @Horsi70

Home Assistant component to arm and disarm the alarm system and read sensor states.

Currently supports:
- alarm control panel, to arm and disarm the Jablotron alarm system
- binary sensor, to separately monitor Jablotron sensors and interactions with panels and app
- MQTT for sensor activities, arming/disarming activites
- Extended configurations, add name and sensor type in YAML file

## Installation
To use this component, copy all scripts to "<home assistant config dir>/custom_components/jablotron_system".
Edit configuration.yaml and add the following lines:

```
jablotron_system:
  port: /dev/hidraw0
  code: 1234
```
Both options 'port' and 'code' are required.
Optional arguments are:
```
  code_arm_required: True
  code_disarm_required: True
  state_topic: "backend/alarm_control_panel/jablotron/state"
  command_topic: "backend/alarm_control_panel/jablotron/set"
```

Note: Because my serial cable presents as a HID device there format is /dev/hidraw[x], others that present as serial may be at /dev/ttyUSB0 or similar. Use the following command line to identify the appropriate device:

```
$ dmesg | grep usb
$ dmesg | grep hid
```

## How it works
- Available platforms (alarm control panel and binary sensors) will be shown on the http(s)://domainname<:8123>/states page.
- The alarm control panel is always available.
- Sensors needs to be scanned for and added into the binary sensor in case they are not found from start
- Discovered (triggered) sensors will be stored in config/jablotron_devices.yaml and get loaded after restart of HA.
- In the jablotron_devices.yaml located in jablotron folder you can customize each sensor:
  - friendly_name : give it a human readable name
  - device_class  : give it a class which matches the device (default is motion)

## Find necessary sensor data
- All sensors will send 2 packets of data when triggered
- First packet is of interest and needs to be analysed in order to be added to binary sensor code
- A sensor will send both on and off info
- On seems to be a hex value that is 2 lower then off value
- On value should be added, if not already existing, to the binary sensor code

## Enable MQTT support
**Alarm_control_panel**

If the mqtt: component has been properly configured on the local host (directly connected to the Jablotron system), the alarm_control_panel will publish states and listen for changed alarm states automatically. You could specify which topics should be used.
- The `state_topic` will be used for announcing new states (MQTT messages will be retained)
- The `command_topic` will be used for receiving incoming states from a remote alarm_control_panel.
- the `data_topic` will be used for sending info on interactions with the panels or applications, who and which. only needed on local host

On both hosts (local and remote) you need to setup an MQTT broker first of course.
- On the local host you need specify topics. For example:
```
jablotron_system:
  port: /dev/hidraw0
  code_arm_required: True
  code_disarm_required: True
  code: !secret jablotron_code
  state_topic: "backend/alarm_control_panel/jablotron/state"
  command_topic: "backend/alarm_control_panel/jablotron/set"
  data_topic: "backend/alarm_control_panel/jablotron/data"
```

- On the remote host you need to setup a [MQTT alarm control panel](https://www.home-assistant.io/components/alarm_control_panel.mqtt/). For example:
```
alarm_control_panel:
  - platform: mqtt
    name: 'Jablotron Alarm'
    state_topic: "backend/alarm_control_panel/jablotron/state"
    command_topic: "backend/alarm_control_panel/jablotron/set"
    code_arm_required: True
    code_disarm_required: True
    code: !secret jablotron_code
```

**Binary_sensor**

In order to publish the states of binary sensors, you could make an automation on the local host like this:
```
automation:
  # if state changes then also update mqtt state
  - alias: 'send to MQTT state'
    initial_state: 'true'
    trigger:
      - platform: state
        entity_id:
          - binary_sensor.jablotron_3
          - binary_sensor.jablotron_4
          - binary_sensor.jablotron_5
    action:
      - service: mqtt.publish
        data_template:
          topic: >
            backend/{{ trigger.entity_id.split('.')[0] }}/{{ trigger.entity_id.split('.')[1] }}/state
          payload: >
            {{ trigger.to_state.state | upper }}
          retain: true
```
On the remote host you need to make MQTT based binary sensors like this:
```
binary_sensor:
  - platform: mqtt
    name: "jablotron_3"
    state_topic: "backend/binary_sensor/jablotron_3/state"
    payload_on: "ON"
    payload_off: "OFF"
    qos: 0
```

## Tested with
- Home Assistant 0.107 installed on RPi 3 model B+ with Hassio
- Jablotron JA-106K-LAN, firmware: LJ60422, hardware: LJ16123
- Jablotron magnetic and PIR (motion) sensors

## Todo list:
- Support for other devices such as (physical) control panel
- Add MQTT support for binary sensors directly in code not to rely on automation
- Improve the "who armed/disarmed" function



